"""Integration tests for the dunning ladder's Celery beat entry points
(``payments.tasks.process_dunning`` / ``process_dunning_for_subscription``).

Drives the real, DI-wired ``DunningService`` -- not a hand-written double --
with the MercadoPago SDK mocked (the same pattern
``payments/tests/views/test_payment_webhooks.py`` uses for the webhook side of
the same adapter) so a retry charge never reaches the network, and
``notification_service`` mocked so the escalating ladder's sends are
assertable without a real email backend.
"""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, PlanLimit, Subscription
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from payments.tasks import process_dunning, process_dunning_for_subscription
from users.models import User


pytestmark = pytest.mark.no_auto_subscription

WEBHOOK_SECRET = "test-webhook-secret"

_DUNNING_SERVICE_MODULE = "payments.services.dunning_service"


@pytest.fixture(autouse=True)
def _fire_on_commit_synchronously():
    """``DunningService`` wraps its notification sends in ``transaction.on_commit``
    -- which never fires inside a plain ``@pytest.mark.django_db`` test (the
    outer test transaction only ever rolls back, never commits). Patched to fire
    immediately, the canonical pattern in this project -- see
    ``calendar_integration/tests/services/test_change_request_notifications.py``.
    """
    with patch(f"{_DUNNING_SERVICE_MODULE}.transaction.on_commit", side_effect=lambda fn: fn()):
        yield


#: A fixed moment to freeze on, so every assertion below is arithmetic a reader
#: can check by hand rather than depending on the date the suite runs.
FREEZE_START = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)


def make_complete_plan(
    limit_values: dict[str, int | None] | None = None,
    *,
    grace_period_days: int | None = None,
) -> BillingPlan:
    """Mirrors ``test_dunning_service.py``'s helper of the same name."""
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=Decimal("50"),
        annual_price=None,
        grace_period_days=grace_period_days,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


def _subscription_for(
    subscription_service,
    organization: Organization,
    plan: BillingPlan,
    *,
    billing_state: str,
    grace_period_ends_at: datetime.datetime | None = None,
) -> Subscription:
    subscription = subscription_service.create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_state = billing_state
    subscription.external_id = "already-on-file"
    subscription.grace_period_ends_at = grace_period_ends_at
    subscription.save(update_fields=["billing_state", "external_id", "grace_period_ends_at"])
    return subscription


def _add_admin_membership(organization: Organization) -> OrganizationMembership:
    return baker.make(
        OrganizationMembership,
        organization=organization,
        user=baker.make(User),
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


def _seed_members(organization: Organization, count: int) -> None:
    """Push usage over the seeded ``free`` plan's ``organization_members`` limit
    (5) so ``DunningService.check_free_fallback`` never short-circuits these
    tests before the ladder gets a chance to run."""
    for _ in range(count):
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=baker.make(User),
            role=OrganizationRole.MEMBER,
            is_active=True,
        )


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def billing_profile(organization):
    billing_address = baker.make(
        "payments.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )
    return baker.make(
        "payments.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


@pytest.fixture
def mercadopago_subscription_adapter():
    with patch(
        "payments.services.subscription_adapters.mercadopago_subscription_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoSubscriptionAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        adapter.sdk.plan().create.return_value = {"response": {"id": "plan-ext-1"}}
        yield adapter


@pytest.fixture
def mock_notification_service():
    return MagicMock()


@pytest.fixture(autouse=True)
def di_overrides(di_container, mercadopago_subscription_adapter, mock_notification_service):
    """Real ``DunningService``/``SubscriptionService`` from the wired container,
    with only the provider SDK and the notification service swapped out --
    proves ``@inject`` on the Celery tasks actually resolves a working service,
    the same concern ``test_metering_tasks.py`` documents for the metering
    tasks."""
    with (
        di_container.subscription_gateway.override(mercadopago_subscription_adapter),
        di_container.notification_service.override(mock_notification_service),
    ):
        yield


@pytest.fixture
def subscription_service(di_container):
    return di_container.subscription_service()


@pytest.mark.django_db
class TestProcessDunningFanOut:
    def test_fans_out_only_grace_and_restricted_subscriptions(
        self, subscription_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        grace_sub = _subscription_for(
            subscription_service, organization, plan, billing_state=BillingState.GRACE
        )

        org2 = baker.make(Organization, parent=None, can_invite_organizations=False)
        baker.make(
            "payments.BillingProfile",
            organization=org2,
            contact_email="billing2@example.com",
            document_type="CPF",
            document_number="98765432100",
            billing_address=baker.make(
                "payments.BillingAddress",
                street_name="St",
                street_number="1",
                city="C",
                state="S",
                country="Co",
                zip_code="00000",
            ),
        )
        restricted_sub = _subscription_for(
            subscription_service, org2, plan, billing_state=BillingState.RESTRICTED
        )

        org3 = baker.make(Organization, parent=None, can_invite_organizations=False)
        baker.make(
            "payments.BillingProfile",
            organization=org3,
            contact_email="billing3@example.com",
            document_type="CPF",
            document_number="11122233300",
            billing_address=baker.make(
                "payments.BillingAddress",
                street_name="St",
                street_number="1",
                city="C",
                state="S",
                country="Co",
                zip_code="00000",
            ),
        )
        _subscription_for(subscription_service, org3, plan, billing_state=BillingState.ACTIVE)

        with patch("payments.tasks.process_dunning_for_subscription.delay") as dispatched:
            process_dunning()

        dispatched_ids = {call.args[0] for call in dispatched.call_args_list}
        assert dispatched_ids == {grace_sub.pk, restricted_sub.pk}


@pytest.mark.django_db
class TestDunningLadder:
    def test_retries_daily_escalates_then_restricts_on_expiry(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        mock_notification_service,
        organization,
        billing_profile,
    ):
        _add_admin_membership(organization)
        _seed_members(organization, 6)  # stay out of the free plan's reach throughout
        plan = make_complete_plan(grace_period_days=3)

        with freeze_time(FREEZE_START):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=FREEZE_START + datetime.timedelta(days=3),
            )

            # Day 0: first retry -- more than a day of grace left, "reminder".
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1
        first_reminder = mock_notification_service.create_notification.call_args_list[-1]
        assert first_reminder.kwargs["context_kwargs"]["urgency"] == "reminder"
        mock_notification_service.reset_mock()

        # A couple of hours later, same day: the per-subscription gate
        # (`MIN_DUNNING_RETRY_INTERVAL`, ~20h) must not fire a second retry.
        with freeze_time(FREEZE_START + datetime.timedelta(hours=2)):
            process_dunning_for_subscription(subscription.pk)

        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1
        mock_notification_service.create_notification.assert_not_called()

        # Day 2, with under a day of grace left: retries again, escalates to
        # "final_warning".
        with freeze_time(FREEZE_START + datetime.timedelta(days=2, hours=1)):
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 2
        final_reminder = mock_notification_service.create_notification.call_args_list[-1]
        assert final_reminder.kwargs["context_kwargs"]["urgency"] == "final_warning"
        mock_notification_service.reset_mock()

        # Past the grace deadline: GRACE -> RESTRICTED, no further retry charge.
        with freeze_time(FREEZE_START + datetime.timedelta(days=3, hours=1)):
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED
        # No new charge attempt on the tick that expires the subscription.
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 2
        mock_notification_service.create_notification.assert_called()

    def test_expires_promptly_even_when_the_retry_throttle_gate_is_still_open(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        mock_notification_service,
        organization,
        billing_profile,
    ):
        """The hourly beat exists specifically so a subscription whose grace
        window elapses moves to RESTRICTED within the hour, not up to
        ``MIN_DUNNING_RETRY_INTERVAL`` (~20h) late because the most recent
        retry happened to land close to the deadline -- see
        ``celerybeat_schedule.py``'s comment and ``DunningService._process_grace``'s
        docstring. Reproduces that timing exactly: a retry two hours before
        ``grace_period_ends_at`` (well inside the throttle window) must not
        suppress the expiry check on the very next tick."""
        _add_admin_membership(organization)
        _seed_members(organization, 6)  # stay out of the free plan's reach
        plan = make_complete_plan(grace_period_days=3)

        with freeze_time(FREEZE_START):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=FREEZE_START + datetime.timedelta(days=3),
            )

        # A retry lands two hours before the deadline -- well inside the ~20h
        # throttle window that would otherwise gate the next tick.
        with freeze_time(FREEZE_START + datetime.timedelta(days=2, hours=22)):
            process_dunning_for_subscription(subscription.pk)
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        retry_count = mercadopago_subscription_adapter.sdk.plan().create.call_count
        assert retry_count == 1

        # One hour later, the deadline has passed. `last_dunning_attempt_at` is
        # only two hours old (nowhere near `MIN_DUNNING_RETRY_INTERVAL`), so if
        # the throttle gated the whole method this tick would silently no-op
        # instead of expiring the subscription.
        with freeze_time(FREEZE_START + datetime.timedelta(days=3, hours=0, minutes=1)):
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED
        # No further charge attempt on the tick that expires the subscription.
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == retry_count

    def test_does_not_retry_once_resolved(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        mock_notification_service,
        organization,
        billing_profile,
        di_container,
    ):
        """Once payment succeeds and ``billing_state`` moves to ``ACTIVE``, the
        beat task must not keep retrying it -- proven at both the fan-out query
        level and by driving a further tick directly and asserting no charge."""
        _add_admin_membership(organization)
        _seed_members(organization, 6)
        plan = make_complete_plan(grace_period_days=3)

        with freeze_time(FREEZE_START):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=FREEZE_START + datetime.timedelta(days=3),
            )
            process_dunning_for_subscription(subscription.pk)

        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1

        # Payment succeeds (e.g. a confirmed subscription-payment webhook).
        dunning_service = di_container.dunning_service()
        dunning_service.resolve_payment_success(subscription)
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE

        with patch("payments.tasks.process_dunning_for_subscription.delay") as dispatched:
            process_dunning()
        assert subscription.pk not in {call.args[0] for call in dispatched.call_args_list}

        with freeze_time(FREEZE_START + datetime.timedelta(days=5)):
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        # No further charge attempt once resolved.
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1

    def test_second_retry_before_the_next_calendar_day_reaches_the_provider_with_a_distinct_key(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        organization,
        billing_profile,
    ):
        """Regression test for a grace/dunning bug: two genuine retry attempts
        must reach the provider with *distinct* idempotency keys even when both
        land on the same UTC calendar day.

        The earlier design bucketed the key by calendar date while the retry
        throttle opened again ``MIN_DUNNING_RETRY_INTERVAL`` (~20h) later, so a
        first attempt landing before 04:00 UTC opened the throttle a second time
        the *same* day and the second charge was silently deduplicated by the
        provider -- a real collection attempt dropped. Both the throttle and the
        key now derive from one retry-bucket ordinal, so a second attempt in a later
        bucket is a genuinely distinct charge.
        """
        _seed_members(organization, 6)
        plan = make_complete_plan(grace_period_days=10)
        # First retry lands at 02:00 UTC -- inside the 00:00-03:59 window that
        # broke the old calendar-day key. Grace began here (grace_start ==
        # grace_period_ends_at - 10 days == this instant).
        early = datetime.datetime(2026, 1, 1, 2, 0, tzinfo=datetime.UTC)

        with freeze_time(early):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=early + datetime.timedelta(days=10),
            )
            process_dunning_for_subscription(subscription.pk)

        update = mercadopago_subscription_adapter.sdk.preapproval().update
        assert update.call_count == 1
        # The idempotency key is threaded through as the third positional arg
        # (`RequestOptions`) -- see `MercadoPagoSubscriptionAdapter.
        # change_subscription_plan`/`_idempotency_options`.
        first_key = update.call_args_list[-1].args[2].custom_headers["x-idempotency-key"]

        # +20h, still 2026-01-01 (22:00 UTC): the gate opens for a new bucket.
        with freeze_time(early + datetime.timedelta(hours=20)):
            process_dunning_for_subscription(subscription.pk)

        assert update.call_count == 2
        second_key = update.call_args_list[-1].args[2].custom_headers["x-idempotency-key"]
        # Same calendar day, yet distinct keys -- a real second charge, not a
        # provider-deduplicated no-op.
        assert first_key.startswith(f"dunning-retry-{subscription.pk}-")
        assert second_key.startswith(f"dunning-retry-{subscription.pk}-")
        assert first_key != second_key

    def test_retry_idempotency_key_is_stable_across_a_same_attempt_redelivery(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        organization,
        billing_profile,
    ):
        """A ``CELERY_TASK_ACKS_LATE`` redelivery of the *same* attempt (whose
        DB write rolled back, so ``last_dunning_attempt_at`` did not persist)
        lands in the same retry bucket and must reuse the same idempotency key,
        so the provider itself refuses the second charge for it."""
        _seed_members(organization, 6)
        plan = make_complete_plan(grace_period_days=10)

        with freeze_time(FREEZE_START):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=FREEZE_START + datetime.timedelta(days=10),
            )
            process_dunning_for_subscription(subscription.pk)

            update = mercadopago_subscription_adapter.sdk.preapproval().update
            assert update.call_count == 1
            first_key = update.call_args_list[-1].args[2].custom_headers["x-idempotency-key"]

            # Simulate the redelivery of an attempt whose transaction rolled
            # back: the stamped `last_dunning_attempt_at` never committed.
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["last_dunning_attempt_at"])

        # A few hours later, still inside the same ~20h bucket.
        with freeze_time(FREEZE_START + datetime.timedelta(hours=3)):
            process_dunning_for_subscription(subscription.pk)

        assert update.call_count == 2
        second_key = update.call_args_list[-1].args[2].custom_headers["x-idempotency-key"]
        assert second_key == first_key


@pytest.mark.django_db
class TestDunningLadderFreeFallback:
    def test_retries_across_grace_then_falls_back_to_free_only_at_expiry(
        self,
        subscription_service,
        mercadopago_subscription_adapter,
        organization,
        billing_profile,
    ):
        """A payment-failure GRACE org that happens to already fit under the free
        plan's ceilings must still get its retry ladder -- it owes money on a
        card on file, so free-fallback is withheld until grace *expiry*, not run
        on the first tick.

        Regression test for an earlier bug: the earlier code checked
        free-fallback before the retry on every GRACE tick and flipped such an
        org to FREE on the very first tick, abandoning a genuine collection
        attempt. Now the ladder runs across the window and only falls to FREE at
        expiry if still unpaid.
        """
        _add_admin_membership(organization)
        # No `_seed_members`: usage already fits under the free plan's limits.
        plan = make_complete_plan(grace_period_days=3)
        with freeze_time(FREEZE_START):
            subscription = _subscription_for(
                subscription_service,
                organization,
                plan,
                billing_state=BillingState.GRACE,
                grace_period_ends_at=FREEZE_START + datetime.timedelta(days=3),
            )
            # First tick, well inside the grace window: a real retry fires
            # instead of an immediate fall to FREE.
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1

        # Past the grace deadline, still unpaid but under free limits: only now
        # does it fall back to FREE (not RESTRICTED), and with no further charge.
        with freeze_time(FREEZE_START + datetime.timedelta(days=3, hours=1)):
            process_dunning_for_subscription(subscription.pk)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.FREE
        assert mercadopago_subscription_adapter.sdk.plan().create.call_count == 1

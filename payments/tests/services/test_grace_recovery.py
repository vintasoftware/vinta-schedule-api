"""Integration tests for grace/restricted recovery via
``SubscriptionService.retry_payment`` -- the service behind
``POST /billing/subscription/retry-payment/``.

The headline test proves four things in one flow:

1. **Ordering** -- the adapter receives ``update_subscription_payment_token``
   strictly before ``pay_outstanding_invoice``. Attaching after charging would
   charge the dead instrument one more time, which is exactly what a payer
   submitting a new card is trying to avoid.
2. **The right primitive** -- ``retry_payment`` drives ``pay_outstanding_invoice``
   and *never* ``change_subscription_plan``. The latter was previously driven
   via ``retry_failed_charge``, which collected $0.00 against a real past-due
   Stripe invoice in production-mode testing -- see
   ``SubscriptionService.retry_payment``'s docstring for the probe numbers.
   This module's ``FakePaymentService`` still implements
   ``change_subscription_plan`` (it is still ``retry_failed_charge``'s own
   primitive, used unchanged by the dunning ladder) specifically so this
   suite can assert it is never reached from ``retry_payment``.
3. **Idempotency namespacing** -- the key that actually reaches the provider is
   ``retry-payment-{pk}-{client_key}``, structurally distinct from the dunning
   ladder's own ``dunning-retry-{pk}-{ordinal}``. A payer paying with a new
   card must never be deduplicated by the provider against the scheduled
   attempt that just failed on the old card.
4. **Webhook-driven recovery** -- the endpoint returns before the charge is
   confirmed (the subscription is still GRACE/RESTRICTED right after
   ``retry_payment`` returns); only once the subscription-payment webhook's
   side effects run does the subscription reach ACTIVE, with its dunning
   bookkeeping cleared and (RESTRICTED only) a calendar resync queued.

Uses a hand-written ``FakePaymentService`` double, exactly like
``test_plan_change.py``/``test_dunning_service.py`` -- what matters here is
*when* the provider is driven and with *what arguments*, not the wire shape of
any one real provider.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.tasks.calendar_sync_tasks import resync_organization_calendars_task
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import RetryPaymentNotApplicableError, SubscriptionNotAttachedError
from payments.models import BillingPlan, PlanLimit, Subscription
from payments.services.dataclasses import CreatedPlan
from payments.services.dunning_service import (
    DunningService,
    dunning_retry_idempotency_key,
    retry_attempt_ordinal,
)
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import (
    SubscriptionService,
    retry_payment_idempotency_key,
)


# This module builds its own Subscription rows directly via SubscriptionService,
# so it opts out of conftest's autouse `provision_default_subscription` --
# mirrors `test_plan_change.py`/`test_dunning_service.py`.
pytestmark = pytest.mark.no_auto_subscription

_DUNNING_MODULE = "payments.services.dunning_service"


def _patch_on_commit():
    """Canonical pattern in this project for testing on_commit-wrapped side
    effects synchronously -- see ``test_dunning_service.py``."""
    return patch(f"{_DUNNING_MODULE}.transaction.on_commit", side_effect=lambda fn: fn())


def make_complete_plan(
    *,
    monthly_price: Decimal = Decimal("50"),
    grace_period_days: int = 10,
) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource``
    member -- what ``assert_plan_is_complete`` requires. Mirrors
    ``test_plan_change.py``/``test_dunning_service.py``'s helper of the same
    name."""
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=monthly_price,
        annual_price=None,
        grace_period_days=grace_period_days,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=0,
            kind=LimitKind.PREPAID,
        )
    return plan


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def billing_profile(organization):
    billing_address = baker.make(
        "vinta_billing.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )
    return baker.make(
        "vinta_billing.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


def _subscription_for(
    organization: Organization,
    plan: BillingPlan,
    *,
    billing_state: str,
    external_id: str = "already-on-file",
    grace_period_ends_at: datetime.datetime | None = None,
    last_dunning_attempt_at: datetime.datetime | None = None,
) -> Subscription:
    subscription = SubscriptionService().create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_state = billing_state
    subscription.external_id = external_id
    subscription.grace_period_ends_at = grace_period_ends_at
    subscription.last_dunning_attempt_at = last_dunning_attempt_at
    subscription.save(
        update_fields=[
            "billing_state",
            "external_id",
            "grace_period_ends_at",
            "last_dunning_attempt_at",
        ]
    )
    return subscription


@dataclass
class FakePaymentService:
    """Hand-written double over the ``PaymentService`` API ``retry_payment``
    drives, precise about *when* each call happens and with what arguments --
    exactly what this module's assertions need.

    Implements ``change_subscription_plan`` too, even though ``retry_payment``
    must never reach it -- ``retry_failed_charge`` (the dunning ladder's own
    primitive) still calls it, and ``TestRetryFailedChargeDunningCallerUnchanged``
    below needs the same double to support both call paths.
    """

    plan_external_id: str = "ext-plan-1"
    #: Every provider-facing call, in the order it happened -- the ordering
    #: assertion (token attached before the charge) reads this list's indices.
    calls: list[str] = field(default_factory=list)
    #: Every idempotency key forwarded to `pay_outstanding_invoice`, in order.
    idempotency_keys: list[str] = field(default_factory=list)
    #: Every `payment_token` forwarded to `update_subscription_payment_token`.
    payment_tokens: list[str] = field(default_factory=list)

    def create_subscription_plan(self, plan, provider: str) -> CreatedPlan:
        self.calls.append("create_subscription_plan")
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id=self.plan_external_id,
        )

    def update_subscription_payment_token(self, subscription, payment_token: str) -> None:
        self.calls.append("update_subscription_payment_token")
        self.payment_tokens.append(payment_token)

    def change_subscription_plan(self, subscription, new_plan, idempotency_key: str = "") -> None:
        self.calls.append("change_subscription_plan")

    def pay_outstanding_invoice(
        self, subscription, payment_token: str, idempotency_key: str = ""
    ) -> None:
        self.calls.append("pay_outstanding_invoice")
        self.idempotency_keys.append(idempotency_key)


@pytest.fixture
def fake_payment_service():
    return FakePaymentService()


@pytest.fixture
def subscription_service(fake_payment_service):
    return SubscriptionService(payment_service=fake_payment_service)


@pytest.fixture
def entitlement_service():
    return EntitlementService()


@pytest.fixture
def dunning_service(subscription_service, entitlement_service):
    return DunningService(
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
        notification_service=MagicMock(),
    )


@pytest.mark.django_db
class TestRetryPaymentGraceRecovery:
    """The headline flow: ``retry_payment`` attaches + charges, and the
    simulated webhook confirms recovery. The webhook is simulated by calling
    ``DunningService.resolve_payment_success`` followed by
    ``SubscriptionService.confirm_plan_change`` directly -- the exact two
    service calls ``PaymentsViewSet._apply_subscription_payment_side_effects``
    makes for an ``APPROVED`` subscription-payment status update -- not a real
    HTTP webhook payload through the DRF view. Matches the convention already
    used by ``test_plan_change.py``/``test_dunning_service.py`` for exercising
    webhook-driven state without re-deriving a provider's wire payload.
    """

    def test_grace_subscription_recovers_via_new_card(
        self,
        subscription_service,
        dunning_service,
        fake_payment_service,
        organization,
        billing_profile,
    ):
        plan = make_complete_plan()
        now = timezone.now()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=now + datetime.timedelta(days=5),
            # No dunning-ladder attempt has landed in this episode yet --
            # mirrors `enter_grace`, which stamps `None`. `retry_payment`
            # never reads this field (see its docstring), so this is just
            # the ordinary starting state, not a precondition for the retry
            # to be allowed.
            last_dunning_attempt_at=None,
        )

        subscription_service.retry_payment(
            subscription, payment_token="tok-new-card", idempotency_key="client-key-1"
        )

        # 1. Ordering: the new instrument is attached strictly before the
        # outstanding balance is collected -- and `change_subscription_plan`
        # (previously used here as a mistaken primitive, still on the double
        # for `retry_failed_charge`'s benefit) is never reached at all.
        assert fake_payment_service.calls.index(
            "update_subscription_payment_token"
        ) < fake_payment_service.calls.index("pay_outstanding_invoice")
        assert "change_subscription_plan" not in fake_payment_service.calls
        assert fake_payment_service.payment_tokens == ["tok-new-card"]

        # 2. Idempotency namespacing: the exact key that reached the provider,
        # derived from the *production* builder -- not a string re-typed here,
        # which could never catch the builder's format drifting (see
        # `retry_payment_idempotency_key`'s docstring).
        expected_key = retry_payment_idempotency_key(subscription.pk, "client-key-1")
        assert fake_payment_service.idempotency_keys == [expected_key]
        # It can never collide with any bucket the dunning ladder would use for
        # this same subscription in this same grace window -- both formats are
        # read from their real production builders (`retry_payment_idempotency_key`
        # / `dunning_retry_idempotency_key`), never re-derived as literal
        # strings, and proven disjoint against every ordinal the ladder could
        # be sitting on right now, not just the one it happens to be on.
        current_ordinal = retry_attempt_ordinal(subscription, timezone.now())
        for ordinal in range(max(current_ordinal - 2, 0), current_ordinal + 3):
            dunning_key = dunning_retry_idempotency_key(subscription.pk, ordinal)
            assert expected_key != dunning_key

        # 3. The endpoint returns before the charge is confirmed -- the
        # subscription is still GRACE right after `retry_payment`.
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE

        # 4. Simulate the approved subscription-payment webhook's side effects.
        with _patch_on_commit():
            dunning_service.resolve_payment_success(subscription)
        subscription_service.confirm_plan_change(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        assert subscription.grace_period_ends_at is None
        assert subscription.last_dunning_attempt_at is None

    def test_restricted_subscription_recovers_and_queues_calendar_resync(
        self,
        subscription_service,
        dunning_service,
        fake_payment_service,
        organization,
        billing_profile,
    ):
        plan = make_complete_plan()
        now = timezone.now()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.RESTRICTED,
            grace_period_ends_at=now - datetime.timedelta(days=1),
            last_dunning_attempt_at=now - datetime.timedelta(days=1),
        )

        subscription_service.retry_payment(
            subscription, payment_token="tok-new-card", idempotency_key="client-key-2"
        )

        assert fake_payment_service.calls.index(
            "update_subscription_payment_token"
        ) < fake_payment_service.calls.index("pay_outstanding_invoice")
        assert "change_subscription_plan" not in fake_payment_service.calls
        expected_key = retry_payment_idempotency_key(subscription.pk, "client-key-2")
        assert fake_payment_service.idempotency_keys == [expected_key]

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED

        # `_trigger_resync_after_recovery` fires only when the prior state was
        # RESTRICTED -- assert the fan-out is actually queued, not merely that
        # billing_state flips.
        with (
            _patch_on_commit(),
            patch.object(resync_organization_calendars_task, "delay") as dispatched,
        ):
            dunning_service.resolve_payment_success(subscription)
        subscription_service.confirm_plan_change(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        assert subscription.grace_period_ends_at is None
        assert subscription.last_dunning_attempt_at is None
        dispatched.assert_called_once_with(organization_id=organization.pk)


@pytest.mark.django_db
class TestRetryPaymentStateGuards:
    """Service-level pin of the two 409 conditions -- the HTTP-shape assertion
    (status code + `code` field) lives in `test_billing_views.py`; this pins
    which exception type `retry_payment` itself raises for each condition."""

    @pytest.mark.parametrize("billing_state", [BillingState.ACTIVE, BillingState.FREE])
    def test_not_grace_or_restricted_is_refused(
        self, subscription_service, organization, billing_profile, billing_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        with pytest.raises(RetryPaymentNotApplicableError):
            subscription_service.retry_payment(
                subscription, payment_token="tok-1", idempotency_key="key-1"
            )

    def test_never_attached_is_refused(self, subscription_service, organization, billing_profile):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization, plan, billing_state=BillingState.GRACE, external_id=""
        )

        with pytest.raises(SubscriptionNotAttachedError):
            subscription_service.retry_payment(
                subscription, payment_token="tok-1", idempotency_key="key-1"
            )

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_downgrade_grace_is_refused_and_drives_no_provider_calls(
        self,
        subscription_service,
        fake_payment_service,
        organization,
        billing_profile,
        billing_state,
    ):
        """BLOCKER 1: `_schedule_downgrade` also drives a
        subscription into GRACE/RESTRICTED, with `pending_plan` set, when an
        org is over its *new, lower* limits -- with no failed charge behind
        it. `retry_payment` must refuse this exactly like `DunningService`
        already refuses to retry it (`is_downgrade_grace`), not charge the
        still-active, *higher* plan (`retry_failed_charge` bills
        `subscription.plan`) while the pending downgrade's later webhook
        confirmation syncs the *lower* plan's limits -- the payer would pay
        the high price and receive the low plan.
        """
        higher_plan = make_complete_plan(monthly_price=Decimal("100"))
        lower_plan = make_complete_plan(monthly_price=Decimal("10"))
        subscription = _subscription_for(organization, higher_plan, billing_state=billing_state)
        subscription.pending_plan = lower_plan
        subscription.pending_billing_interval = subscription.billing_interval
        subscription.pending_plan_effective_at = timezone.now() + datetime.timedelta(days=20)
        subscription.save(
            update_fields=[
                "pending_plan",
                "pending_billing_interval",
                "pending_plan_effective_at",
            ]
        )

        with pytest.raises(RetryPaymentNotApplicableError):
            subscription_service.retry_payment(
                subscription, payment_token="tok-new-card", idempotency_key="key-1"
            )

        # Zero provider calls -- refused before either the token attach or
        # the charge, not merely before the charge.
        assert fake_payment_service.calls == []


@pytest.mark.django_db
class TestRetryPaymentIdempotencyKeyDedup:
    """`retry_payment` dedups on the *caller's* `idempotency_key`, not on the
    dunning bucket. The row lock (see the
    method's docstring) only serializes concurrent calls -- it does not
    decide whether a second call is a duplicate or a deliberate new attempt.
    That decision is delegated entirely to the provider via the namespaced
    idempotency key: same client key -> same namespaced key -> the provider
    collapses it into one charge; different client key -> different
    namespaced key -> a deliberately distinct attempt, deliberately allowed
    to reach the provider.

    A bucket-based self-throttle was tried and reverted: `last_dunning
    _attempt_at` is also stamped by the dunning ladder
    (`DunningService._retry_charge_and_notify`), and `MIN_DUNNING_RETRY
    _INTERVAL` is 20 hours, so gating this endpoint on that field refused the
    payer exactly when they were trying to fix the failure that stamped it --
    see `test_ladder_stamp_does_not_block_a_user_retry` below, which pins the
    regression that motivated dropping it.
    """

    def test_repeat_retry_with_same_idempotency_key_reaches_provider_under_same_namespaced_key(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        """A double-click, or a client retrying a slow response without
        regenerating its key, must collapse into one charge at the provider.
        `retry_payment` itself is not required to no-op locally -- it is the
        *provider* that dedups two calls carrying the same namespaced key."""
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
            last_dunning_attempt_at=None,
        )

        subscription_service.retry_payment(
            subscription, payment_token="tok-card-A", idempotency_key="client-key-1"
        )
        subscription_service.retry_payment(
            subscription, payment_token="tok-card-A", idempotency_key="client-key-1"
        )

        expected_key = retry_payment_idempotency_key(subscription.pk, "client-key-1")
        assert fake_payment_service.idempotency_keys == [expected_key, expected_key]

    def test_retry_with_different_idempotency_key_is_allowed_under_a_distinct_namespaced_key(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        """Deliberate, not a bug: a payer whose first replacement card was
        declined must be able to try a second card immediately, in the same
        retry bucket the first attempt landed in. This is indistinguishable
        from a duplicate submission by any signal `retry_payment` has *except*
        the client's own idempotency key -- so that key, and only that key,
        is what tells the two apart."""
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
            last_dunning_attempt_at=None,
        )

        subscription_service.retry_payment(
            subscription, payment_token="tok-card-declined", idempotency_key="client-key-1"
        )
        subscription_service.retry_payment(
            subscription, payment_token="tok-card-B", idempotency_key="client-key-2"
        )

        first_key = retry_payment_idempotency_key(subscription.pk, "client-key-1")
        second_key = retry_payment_idempotency_key(subscription.pk, "client-key-2")
        assert first_key != second_key
        assert fake_payment_service.idempotency_keys == [first_key, second_key]
        assert fake_payment_service.payment_tokens == ["tok-card-declined", "tok-card-B"]

    def test_ladder_stamp_does_not_block_a_user_retry(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        """The regression this change fixes: the dunning ladder stamping
        `last_dunning_attempt_at` on a scheduled retry (simulated here by
        setting it to `now`, the most recent possible bucket) must never
        refuse a payer's own `retry_payment` call -- that field is no longer
        read by `retry_payment` at all. Before this change, this exact setup
        raised `RetryPaymentNotApplicableError`."""
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
            # The ladder just ticked on the payer's dead card and stamped
            # this field moments ago -- the payer is now submitting a new
            # card in direct response to that failure.
            last_dunning_attempt_at=timezone.now(),
        )

        subscription_service.retry_payment(
            subscription, payment_token="tok-new-card", idempotency_key="client-key-1"
        )

        expected_key = retry_payment_idempotency_key(subscription.pk, "client-key-1")
        assert fake_payment_service.idempotency_keys == [expected_key]
        assert fake_payment_service.payment_tokens == ["tok-new-card"]


@pytest.mark.django_db
class TestRetryFailedChargeDunningCallerUnchanged:
    """Regression pin: `retry_failed_charge`'s existing blank-`external_id`
    behavior (log and return the subscription unchanged, no provider call) is
    exactly what `DunningService`'s dunning-ladder caller
    (`_retry_charge_and_notify`) still gets -- `retry_payment` does not touch
    this method's contract, only wraps it with a stricter, user-facing guard of
    its own (`SubscriptionNotAttachedError`, raised earlier in `retry_payment`
    and never reached by the dunning caller)."""

    def test_blank_external_id_returns_unchanged_without_driving_the_provider(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization, plan, billing_state=BillingState.GRACE, external_id=""
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result is subscription
        assert fake_payment_service.calls == []

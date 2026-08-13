"""Unit tests for the grace/dunning state machine.

Two things this module exists to pin:

- **The diagram is exhaustively enforced.** ``TestBillingStateMachineDiagram``
  drives every ``(from_state, to_state)`` pair the closed ``BillingState`` set
  can produce (5x5 = 25) through ``billing_state_machine.transition_billing_state``
  and asserts it is permitted exactly when it is on the lifecycle diagram and
  raises otherwise. Not a sample of the diagram's edges, all of them, plus
  every non-edge.
- **``DunningService``'s higher-level methods are the only way the webhook
  handlers and the beat task touch ``billing_state``.** Every test below drives
  those methods, never ``subscription.billing_state = ...`` directly.

Also carries the two hard constraints:
``TestConstraint1PaymentMethodStaysTrueInGrace`` and
``TestConstraint2ClearsPlanChangePendingConfirmation``.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.utils import timezone

import pytest
from model_bakery import baker
from vintasend.constants import NotificationTypes

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.services import sync_membership_groups_from_role
from organizations.tests.helpers import make_membership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.constants import PaymentProviders
from payments.exceptions import (
    ChargeDeclinedError,
    CollectionNotSupportedError,
    IllegalBillingStateTransitionError,
    NoOutstandingBalanceError,
)
from payments.models import BillingPlan, PaymentMethod, PlanLimit, Subscription
from payments.services.billing_state_machine import (
    LEGAL_BILLING_STATE_TRANSITIONS,
    transition_billing_state,
)
from payments.services.dataclasses import CreatedPlan
from payments.services.dunning_service import (
    DunningService,
    dunning_retry_idempotency_key,
    retry_attempt_ordinal,
)
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService
from users.models import User


pytestmark = pytest.mark.no_auto_subscription

_MODULE = "payments.services.dunning_service"

ALL_BILLING_STATES = list(BillingState)


def _patch_on_commit():
    """Canonical pattern in this project for testing on_commit-wrapped side
    effects synchronously -- see
    ``calendar_integration/tests/services/test_change_request_notifications.py``."""
    return patch(f"{_MODULE}.transaction.on_commit", side_effect=lambda fn: fn())


def make_complete_plan(
    limit_values: dict[str, int | None] | None = None,
    *,
    monthly_price: Decimal = Decimal("0"),
    grace_period_days: int | None = None,
) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource``
    member -- what ``assert_plan_is_complete`` requires. Mirrors
    ``test_plan_change.py``'s helper of the same name."""
    limit_values = limit_values or {}
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
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


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


def _subscription_for(
    organization: Organization,
    plan: BillingPlan,
    *,
    billing_state: str = BillingState.ACTIVE,
    external_id: str = "already-on-file",
    grace_period_ends_at: datetime.datetime | None = None,
    plan_change_pending_confirmation: bool = False,
) -> Subscription:
    subscription = SubscriptionService().create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_state = billing_state
    subscription.external_id = external_id
    subscription.grace_period_ends_at = grace_period_ends_at
    subscription.plan_change_pending_confirmation = plan_change_pending_confirmation
    subscription.save(
        update_fields=[
            "billing_state",
            "external_id",
            "grace_period_ends_at",
            "plan_change_pending_confirmation",
        ]
    )
    return subscription


def _add_admin_membership(organization: Organization) -> OrganizationMembership:
    """A recipient for ``DunningService``'s notifications --
    ``OrganizationMembershipQuerySet.billing_recipients`` reads active
    memberships holding ``payments.manage_billing``, and a bare
    ``baker.make(Organization, ...)`` (unlike
    ``OrganizationService.create_organization``) creates none on its own.

    ``sync_membership_groups_from_role`` is what every live write path calls to
    keep the groups in step with ``role``; ``baker.make`` bypasses it, so this
    calls it by hand. Phase 6 deletes the shim and this call with it."""
    membership = make_membership(
        organization=organization,
        user=baker.make(User),
        role=OrganizationRole.ADMIN,
        is_active=True,
    )
    sync_membership_groups_from_role(membership)
    return membership


def _seed_members(organization: Organization, count: int) -> None:
    """``count`` seat-occupying members, e.g. to push usage over the seeded
    ``free`` plan's ``organization_members`` limit (5) so
    ``DunningService.check_free_fallback`` does not short-circuit a test that
    means to exercise the retry path instead."""
    for _ in range(count):
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=baker.make(User),
            role=OrganizationRole.MEMBER,
            is_active=True,
        )


@dataclass
class FakePaymentService:
    """Same hand-written double ``test_plan_change.py`` uses -- precise about
    *when* the provider is driven, not its wire shape.

    ``raise_collection_not_supported``/``raise_no_outstanding_balance`` let a
    test drive ``pay_outstanding_invoice`` down the two error branches
    ``SubscriptionService.retry_failed_charge`` (Billing API Contract
    Hardening, Phase 5) must tolerate: MercadoPago's typed refusal (falls back
    to ``change_subscription_plan``) and "nothing owed right now" (swallowed,
    the tick is a no-op).
    """

    plan_external_id: str = "ext-plan-1"
    calls: list[str] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)
    create_subscription_plan_providers: list[str] = field(default_factory=list)
    pay_outstanding_invoice_calls: list[tuple[str, str]] = field(default_factory=list)
    #: Rule A pin (Payment Provider Selection, Phase 4): which provider each
    #: `pay_outstanding_invoice` call actually resolved from `subscription`'s
    #: own stored `payment_provider` -- recorded separately from
    #: `pay_outstanding_invoice_calls` (token, key) so a test can assert the
    #: retry drove the *provider* it was supposed to, not only its arguments.
    pay_outstanding_invoice_providers: list[str] = field(default_factory=list)
    raise_collection_not_supported: bool = False
    raise_no_outstanding_balance: bool = False
    raise_charge_declined: bool = False

    # `provider` is required, matching `PaymentService.create_subscription_plan`'s
    # real signature -- see `test_plan_change.py`'s double.
    def create_subscription_plan(self, plan, provider: str) -> CreatedPlan:
        self.calls.append("create_subscription_plan")
        self.create_subscription_plan_providers.append(provider)
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id=self.plan_external_id,
        )

    def change_subscription_plan(self, subscription, new_plan, idempotency_key: str = "") -> None:
        self.calls.append("change_subscription_plan")
        self.idempotency_keys.append(idempotency_key)

    def pay_outstanding_invoice(
        self, subscription, payment_token: str = "", idempotency_key: str = ""
    ) -> None:
        self.calls.append("pay_outstanding_invoice")
        self.pay_outstanding_invoice_calls.append((payment_token, idempotency_key))
        self.pay_outstanding_invoice_providers.append(subscription.payment_provider)
        if self.raise_collection_not_supported:
            raise CollectionNotSupportedError(
                subscription.id, "MercadoPago has no verified collection primitive"
            )
        if self.raise_no_outstanding_balance:
            raise NoOutstandingBalanceError(subscription.id)
        if self.raise_charge_declined:
            raise ChargeDeclinedError(subscription.id, "Your card was declined.")


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
def mock_notification_service():
    return MagicMock()


@pytest.fixture
def dunning_service(subscription_service, entitlement_service, mock_notification_service):
    return DunningService(
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
        notification_service=mock_notification_service,
    )


# ---------------------------------------------------------------------------
# The diagram, exhaustively
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBillingStateMachineDiagram:
    def test_legal_transitions_matches_the_diagram_exactly(self):
        """A regression pin: if this set drifts from the spec's mermaid diagram,
        this is the first thing to fail, before any behavioral test does."""
        assert LEGAL_BILLING_STATE_TRANSITIONS == frozenset(
            {
                (BillingState.FREE, BillingState.ACTIVE),
                (BillingState.ACTIVE, BillingState.ACTIVE),
                (BillingState.ACTIVE, BillingState.GRACE),
                (BillingState.FREE, BillingState.GRACE),
                (BillingState.GRACE, BillingState.ACTIVE),
                (BillingState.GRACE, BillingState.FREE),
                (BillingState.GRACE, BillingState.RESTRICTED),
                (BillingState.RESTRICTED, BillingState.ACTIVE),
                (BillingState.RESTRICTED, BillingState.FREE),
                (BillingState.ACTIVE, BillingState.CANCELLED),
                (BillingState.FREE, BillingState.CANCELLED),
                (BillingState.GRACE, BillingState.CANCELLED),
                (BillingState.RESTRICTED, BillingState.CANCELLED),
                (BillingState.CANCELLED, BillingState.FREE),
            }
        )

    @pytest.mark.parametrize(
        "from_state,to_state",
        [(f, t) for f in ALL_BILLING_STATES for t in ALL_BILLING_STATES],
        ids=[f"{f.value}->{t.value}" for f in ALL_BILLING_STATES for t in ALL_BILLING_STATES],
    )
    def test_every_pair_of_the_closed_state_set(
        self, organization, billing_profile, from_state, to_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=from_state)
        is_legal = (
            from_state == to_state or (from_state, to_state) in LEGAL_BILLING_STATE_TRANSITIONS
        )

        if is_legal:
            _, changed = transition_billing_state(subscription, to_state)
            subscription.refresh_from_db()
            assert subscription.billing_state == to_state
            assert changed == (from_state != to_state)
        else:
            with pytest.raises(IllegalBillingStateTransitionError):
                transition_billing_state(subscription, to_state)
            subscription.refresh_from_db()
            assert subscription.billing_state == from_state


# ---------------------------------------------------------------------------
# enter_grace
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEnterGrace:
    def test_active_to_grace_stamps_grace_period_ends_at_from_the_plan(
        self, dunning_service, organization, billing_profile
    ):
        plan = make_complete_plan(grace_period_days=10)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)
        before = timezone.now()

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert subscription.grace_period_ends_at is not None
        assert subscription.grace_period_ends_at >= before + datetime.timedelta(days=10)

    def test_free_to_grace_is_also_legal(self, dunning_service, organization, billing_profile):
        plan = make_complete_plan(grace_period_days=5)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.FREE)

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE

    def test_falls_back_to_the_settings_default_when_plan_has_no_grace_period_days(
        self, dunning_service, organization, billing_profile
    ):
        plan = make_complete_plan(grace_period_days=None)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)
        before = timezone.now()

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        expected_floor = before + datetime.timedelta(
            days=settings.BILLING_DEFAULT_GRACE_PERIOD_DAYS
        )
        assert subscription.grace_period_ends_at >= expected_floor

    def test_idempotent_does_not_restamp_or_renotify(
        self, dunning_service, mock_notification_service, organization, billing_profile
    ):
        plan = make_complete_plan(grace_period_days=10)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)
        subscription.refresh_from_db()
        first_deadline = subscription.grace_period_ends_at
        mock_notification_service.reset_mock()

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)
        subscription.refresh_from_db()

        assert subscription.grace_period_ends_at == first_deadline
        mock_notification_service.create_notification.assert_not_called()

    @pytest.mark.parametrize(
        "billing_state",
        [BillingState.RESTRICTED, BillingState.CANCELLED],
    )
    def test_tolerates_states_past_grace_without_raising_or_moving_backwards(
        self, dunning_service, organization, billing_profile, billing_state
    ):
        """A failed charge can legitimately arrive while a subscription is
        already further along the ladder (RESTRICTED) or out of it entirely
        (CANCELLED) -- neither is a source for this edge on the diagram, so this
        must be a tolerant no-op, not a raise, on a real webhook path."""
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        result = dunning_service.enter_grace(subscription)

        assert result.billing_state == billing_state

    def test_sends_in_app_and_email_notification(
        self, dunning_service, mock_notification_service, organization, billing_profile
    ):
        _add_admin_membership(organization)
        plan = make_complete_plan(grace_period_days=7)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        notification_types = {
            call.kwargs["notification_type"]
            for call in mock_notification_service.create_notification.call_args_list
        }
        assert notification_types == {NotificationTypes.IN_APP.value, NotificationTypes.EMAIL.value}


@pytest.mark.django_db
class TestConstraint2ClearsPlanChangePendingConfirmation:
    def test_enter_grace_clears_the_flag(self, dunning_service, organization, billing_profile):
        """``_initiate_upgrade`` sets ``plan_change_pending_confirmation`` and
        only clears it on an APPROVED webhook. A first-upgrade whose charge
        *fails* never reaches that branch. The failed-charge path must clear it
        here, or the org is stuck unable to request a different plan
        (``UnconfirmedPlanChangeError``)."""
        plan = make_complete_plan(grace_period_days=7)
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.ACTIVE,
            plan_change_pending_confirmation=True,
        )

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.plan_change_pending_confirmation is False


# ---------------------------------------------------------------------------
# resolve_payment_success
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolvePaymentSuccess:
    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_moves_to_active_and_clears_grace_bookkeeping(
        self, dunning_service, organization, billing_profile, billing_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=billing_state,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=1),
        )
        subscription.last_dunning_attempt_at = timezone.now()
        subscription.save(update_fields=["last_dunning_attempt_at"])

        result = dunning_service.resolve_payment_success(subscription)

        assert result.billing_state == BillingState.ACTIVE
        assert result.grace_period_ends_at is None
        assert result.last_dunning_attempt_at is None

    @pytest.mark.parametrize(
        "billing_state", [BillingState.ACTIVE, BillingState.FREE, BillingState.CANCELLED]
    )
    def test_noop_outside_grace_or_restricted(
        self, dunning_service, organization, billing_profile, billing_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        result = dunning_service.resolve_payment_success(subscription)

        assert result.billing_state == billing_state


# ---------------------------------------------------------------------------
# expire_grace
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExpireGrace:
    def test_grace_to_restricted_notifies_when_usage_does_not_fit_free(
        self, dunning_service, mock_notification_service, organization, billing_profile
    ):
        _add_admin_membership(organization)
        _seed_members(organization, 6)  # over the free plan's limit -- no free fallback
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.GRACE)

        with _patch_on_commit():
            result = dunning_service.expire_grace(subscription)

        assert result.billing_state == BillingState.RESTRICTED
        mock_notification_service.create_notification.assert_called()

    def test_grace_to_free_at_expiry_when_usage_fits(
        self, dunning_service, organization, billing_profile
    ):
        """At grace expiry an org whose usage now fits under the free plan's
        ceilings falls back to FREE rather than RESTRICTED -- the free-fallback
        the ladder deliberately withholds mid-window is resolved here, once the
        window has elapsed unpaid."""
        _seed_members(organization, 2)  # well under the free plan's limit of 5
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=1),
        )
        subscription.last_dunning_attempt_at = timezone.now()
        subscription.save(update_fields=["last_dunning_attempt_at"])

        with _patch_on_commit():
            result = dunning_service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert result.billing_state == BillingState.FREE
        assert subscription.grace_period_ends_at is None
        assert subscription.last_dunning_attempt_at is None

    @pytest.mark.parametrize(
        "billing_state",
        [BillingState.ACTIVE, BillingState.FREE, BillingState.RESTRICTED, BillingState.CANCELLED],
    )
    def test_noop_outside_grace(
        self,
        dunning_service,
        mock_notification_service,
        organization,
        billing_profile,
        billing_state,
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        result = dunning_service.expire_grace(subscription)

        assert result.billing_state == billing_state
        mock_notification_service.create_notification.assert_not_called()


@pytest.mark.django_db
class TestDowngradeOriginatedGrace:
    """A GRACE episode driven by ``SubscriptionService._schedule_downgrade``
    (rather than a failed charge) resolves differently. There is no charge to
    retry, and expiry checks the just-applied (lower) limits rather than the
    catalog ``free`` plan. ``pending_plan`` being set is what marks a
    subscription this way (``is_downgrade_grace``). These tests
    set it directly rather than driving the full ``request_plan_change`` flow,
    which ``test_plan_change.py``'s ``TestDowngradeDrivesGraceForTheSweep``
    already covers end to end.
    """

    def test_resolves_to_active_when_usage_fits_the_new_limits(
        self, dunning_service, organization, billing_profile
    ):
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 5})
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=1),
        )
        subscription.pending_plan = plan
        subscription.save(update_fields=["pending_plan"])
        _seed_members(organization, 2)  # well under 5

        with _patch_on_commit():
            result = dunning_service.expire_grace(subscription)

        subscription.refresh_from_db()
        assert result.billing_state == BillingState.ACTIVE
        assert subscription.grace_period_ends_at is None

    def test_resolves_to_restricted_when_still_over_the_new_limits(
        self, dunning_service, mock_notification_service, organization, billing_profile
    ):
        _add_admin_membership(organization)
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 1})
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=1),
        )
        subscription.pending_plan = plan
        subscription.save(update_fields=["pending_plan"])
        _seed_members(organization, 6)  # over 1

        with _patch_on_commit():
            result = dunning_service.expire_grace(subscription)

        assert result.billing_state == BillingState.RESTRICTED
        mock_notification_service.create_notification.assert_called()

    def test_process_subscription_does_not_retry_a_charge_mid_window(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        """No charge to retry for a downgrade -- unlike a payment-failure
        grace, a tick inside the window must not drive the provider at all."""
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 1})
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        subscription.pending_plan = plan
        subscription.save(update_fields=["pending_plan"])
        _seed_members(organization, 6)

        with _patch_on_commit():
            dunning_service.process_subscription(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert fake_payment_service.calls == []

    def test_still_expires_on_the_deadline_despite_never_having_retried(
        self, dunning_service, organization, billing_profile
    ):
        """A downgrade-origin grace with a deadline in the past must still
        expire, even though ``process_subscription`` never drove a charge retry
        for it. This is the dead edge that was left unswept."""
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 1})
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() - datetime.timedelta(hours=1),
        )
        subscription.pending_plan = plan
        subscription.save(update_fields=["pending_plan"])
        _seed_members(organization, 6)  # still over the new limit

        with _patch_on_commit():
            dunning_service.process_subscription(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.RESTRICTED


# ---------------------------------------------------------------------------
# check_free_fallback
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckFreeFallback:
    """Resolved against the catalog's seeded ``free`` plan (``payments.migrations
    .0007_seed_billing_plans``), whose placeholder ``organization_members`` limit
    is 5 -- not the rollout's ``unlimited`` (every limit NULL, would trivially
    "fit" and short-circuit the whole ladder on its first tick)."""

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_falls_back_to_free_when_usage_fits(
        self, dunning_service, organization, billing_profile, billing_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=billing_state,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=1),
        )
        _seed_members(organization, 2)  # well under the free plan's limit of 5

        result = dunning_service.check_free_fallback(subscription)

        subscription.refresh_from_db()
        assert result is True
        assert subscription.billing_state == BillingState.FREE
        assert subscription.grace_period_ends_at is None
        # The nominal catalog plan is untouched -- only billing_state moved.
        assert subscription.plan_id == plan.pk

    def test_stays_in_grace_when_usage_does_not_fit(
        self, dunning_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.GRACE)
        _seed_members(organization, 6)  # over the free plan's limit of 5

        result = dunning_service.check_free_fallback(subscription)

        subscription.refresh_from_db()
        assert result is False
        assert subscription.billing_state == BillingState.GRACE

    def test_noop_outside_grace_or_restricted(self, dunning_service, organization, billing_profile):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)

        assert dunning_service.check_free_fallback(subscription) is False


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancel:
    @pytest.mark.parametrize(
        "billing_state",
        [
            BillingState.ACTIVE,
            BillingState.FREE,
            BillingState.GRACE,
            BillingState.RESTRICTED,
        ],
    )
    def test_cancels_from_every_live_state(
        self, dunning_service, organization, billing_profile, billing_state
    ):
        """The product's cancel action is offered from any live state, so all
        four are legal cancellation sources (only ``ACTIVE -> CANCELLED`` is
        drawn on the spec diagram; the rest are the product edges the machine
        carries beyond it -- see ``LEGAL_BILLING_STATE_TRANSITIONS``)."""
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        result = dunning_service.cancel(subscription)

        assert result.billing_state == BillingState.CANCELLED


# ---------------------------------------------------------------------------
# Constraint 1 -- has_payment_method stays True through GRACE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConstraint1PaymentMethodStaysTrueInGrace:
    def test_has_payment_method_survives_entering_grace(
        self, dunning_service, entitlement_service, organization, billing_profile
    ):
        """``enter_grace`` must never touch ``PaymentMethod`` -- a failed charge
        says nothing about whether the card is still attached. An organization
        with a card on file must keep reading ``has_payment_method() is True``
        after moving to GRACE, so it keeps accruing postpaid usage; the dunning
        ladder, not the postpaid guard, is what escalates it (Constraint 1)."""
        plan = make_complete_plan(grace_period_days=7)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)
        baker.make(
            PaymentMethod,
            organization=organization,
            provider=PaymentProviders.MERCADOPAGO,
            external_id="card-on-file",
            is_active=True,
        )
        assert entitlement_service.has_payment_method(organization) is True

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert entitlement_service.has_payment_method(organization) is True
        assert PaymentMethod.objects.filter(organization=organization, is_active=True).count() == 1


# ---------------------------------------------------------------------------
# process_subscription dispatch
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessSubscriptionDispatch:
    def test_dispatches_grace_to_the_grace_handler(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        _seed_members(organization, 6)  # over the free plan's limit -- no free fallback

        with _patch_on_commit():
            dunning_service.process_subscription(subscription)

        subscription.refresh_from_db()
        # Billing API Contract Hardening, Phase 5: the ladder's Stripe retry
        # drives `pay_outstanding_invoice` -- the primitive that actually
        # collects -- never `change_subscription_plan` (proven $0.00 against a
        # real past-due invoice, see `retry_failed_charge`'s docstring).
        assert "pay_outstanding_invoice" in fake_payment_service.calls
        assert "change_subscription_plan" not in fake_payment_service.calls
        # Called with an empty token (the ladder has no new instrument to
        # attach -- see `BaseSubscriptionAdapter.pay_outstanding_invoice`) and
        # under the ladder's own bucketed idempotency key -- derived the same
        # way `_retry_charge_and_notify` derives it, never a re-typed literal
        # (see `dunning_retry_idempotency_key`'s docstring for why).
        current_ordinal = retry_attempt_ordinal(subscription, timezone.now())
        assert fake_payment_service.pay_outstanding_invoice_calls == [
            ("", dunning_retry_idempotency_key(subscription.pk, current_ordinal))
        ]
        assert subscription.last_dunning_attempt_at is not None
        # Payment Provider Selection, Phase 4, Rule A: the retry drives the
        # provider resolved from the *subscription's own* stored provider.
        # `billing_profile` above is unpinned, so `create_subscription_for_organization`
        # resolved this subscription onto `settings.DEFAULT_PAYMENT_PROVIDER`
        # (`stripe`) -- asserted as the literal rather than by reading the column
        # back, which would pass against any value the routing happened to use.
        assert settings.DEFAULT_PAYMENT_PROVIDER == PaymentProviders.STRIPE
        assert subscription.payment_provider == PaymentProviders.STRIPE
        # SHOULD-FIX 7: `FakePaymentService.pay_outstanding_invoice` (above)
        # only records `(payment_token, idempotency_key)`, not which provider
        # it was invoked for -- the assertion above alone cannot tell a
        # Stripe retry apart from a MercadoPago one recording the same
        # `("", key)` shape. `test_dunning_schedule.py`'s
        # `TestDunningTickToleratesADeclinedStripeCharge` still pins this
        # end-to-end through the real `StripeSubscriptionAdapter`, so this is
        # a weakening of that guarantee, not a hole in it -- but pin it here
        # too, at the double.
        assert fake_payment_service.pay_outstanding_invoice_providers == [PaymentProviders.STRIPE]
        # `pay_outstanding_invoice` collects directly, no plan/price object is
        # minted for this path -- unlike the MercadoPago fallback below.
        assert fake_payment_service.create_subscription_plan_providers == []

    def test_retry_drives_the_subscriptions_own_provider_not_the_organizations_pin(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        """Rule A, discriminating case: a subscription stamped ``mercadopago``
        under an organization pinned to ``stripe`` must retry through
        MercadoPago. Reading the provider back off the subscription (as the
        assertion above necessarily does for the agreeing case) cannot tell the
        two rules apart; deliberately disagreeing them can.

        ``raise_collection_not_supported`` simulates
        ``MercadoPagoSubscriptionAdapter.pay_outstanding_invoice``'s real,
        typed refusal (Billing API Contract Hardening, Phase 5) -- exercising
        `retry_failed_charge`'s fallback path, which is the one that still
        drives `_ensure_provider_plan`/`create_subscription_plan` against the
        subscription's own provider."""
        fake_payment_service.raise_collection_not_supported = True
        billing_profile.payment_provider = PaymentProviders.STRIPE
        billing_profile.save(update_fields=["payment_provider"])
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        subscription.payment_provider = PaymentProviders.MERCADOPAGO
        subscription.save(update_fields=["payment_provider"])
        _seed_members(organization, 6)

        with _patch_on_commit():
            dunning_service.process_subscription(subscription)

        assert fake_payment_service.create_subscription_plan_providers == [
            PaymentProviders.MERCADOPAGO
        ]
        # `pay_outstanding_invoice` is tried first (and refused), and the
        # fallback still lands on `change_subscription_plan` -- MercadoPago's
        # ladder is byte-identical to before this phase.
        assert fake_payment_service.calls == [
            "pay_outstanding_invoice",
            "create_subscription_plan",
            "change_subscription_plan",
        ]

    def test_collection_not_supported_reraises_for_a_non_mercadopago_provider(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        """SHOULD-FIX 1: the ``CollectionNotSupportedError`` -> fallback path
        is a temporary concession pinned to MercadoPago's specific, unverified
        state (see ``retry_failed_charge``'s docstring) -- it must not also
        silently route a Stripe (or any other) subscription into
        ``_ensure_provider_plan`` + ``change_subscription_plan``, the exact
        operation a live Stripe probe proved collects **$0.00** against a
        real past-due invoice. Nothing raises ``CollectionNotSupportedError``
        for Stripe today (only ``MercadoPagoSubscriptionAdapter`` does), so
        this is the latent-but-real case the reviewer flagged: it must fail
        loudly rather than fall back on a guess."""
        fake_payment_service.raise_collection_not_supported = True
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.save(update_fields=["payment_provider"])
        _seed_members(organization, 6)

        with _patch_on_commit(), pytest.raises(CollectionNotSupportedError):
            dunning_service.process_subscription(subscription)

        # No fallback drove `_ensure_provider_plan`/`change_subscription_plan`
        # against Stripe -- the $0.00-collection operation this phase exists
        # to keep the ladder away from.
        assert fake_payment_service.calls == ["pay_outstanding_invoice"]

    def test_dispatches_restricted_to_the_free_fallback_check_only(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.RESTRICTED)

        dunning_service.process_subscription(subscription)

        # No charge retry for RESTRICTED -- the write-block prevents it; only
        # the free-fallback check runs.
        assert fake_payment_service.calls == []

    @pytest.mark.parametrize(
        "billing_state", [BillingState.ACTIVE, BillingState.FREE, BillingState.CANCELLED]
    )
    def test_noop_for_states_outside_the_dunning_flow(
        self, dunning_service, fake_payment_service, organization, billing_profile, billing_state
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=billing_state)

        dunning_service.process_subscription(subscription)

        assert fake_payment_service.calls == []


# ---------------------------------------------------------------------------
# retry_failed_charge -- non-fatal outcomes the ladder must tolerate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRetryFailedChargeTolerantOfNonFatalOutcomes:
    """Billing API Contract Hardening, Phase 5: ``retry_failed_charge`` is a
    background beat tick (``CELERY_TASK_ACKS_LATE``) -- it must never raise
    out of a legitimate "nothing to do" outcome, unlike ``retry_payment``
    (the user-facing endpoint), which surfaces the analogous 409s instead."""

    def test_blank_external_id_returns_unchanged_without_raising(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            external_id="",
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result == subscription
        assert fake_payment_service.calls == []

    def test_no_outstanding_balance_returns_unchanged_without_raising(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        fake_payment_service.raise_no_outstanding_balance = True
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result == subscription
        assert fake_payment_service.calls == ["pay_outstanding_invoice"]
        # No fallback to `change_subscription_plan` here -- "nothing owed" is
        # not "the provider can't collect at all", it is simply a no-op tick.
        assert "change_subscription_plan" not in fake_payment_service.calls

    def test_charge_declined_returns_unchanged_without_raising(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        """Billing API Contract Hardening, Phase 5 live-probe BLOCKER: a card
        still dead on the retry -- the *common* dunning-tick outcome -- must
        not raise out of ``retry_failed_charge``. Left unhandled, this would
        reach ``process_dunning_for_subscription`` and, per that task's own
        docstring, redeliver identically forever."""
        fake_payment_service.raise_charge_declined = True
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result == subscription
        assert fake_payment_service.calls == ["pay_outstanding_invoice"]
        # No fallback to `change_subscription_plan` here either -- a declined
        # charge is not "the provider can't collect at all"
        # (`CollectionNotSupportedError`), it is a real attempt that failed;
        # the state transition happens later, off the provider's own webhook.
        assert "change_subscription_plan" not in fake_payment_service.calls

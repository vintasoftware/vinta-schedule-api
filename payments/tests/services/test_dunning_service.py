"""Unit tests for the grace/dunning state machine (Phase 10).

Two things this module exists to pin, per the phase's own warning about its
recurring failure shape:

- **The diagram is exhaustively enforced.** ``TestBillingStateMachineDiagram``
  drives every ``(from_state, to_state)`` pair the closed ``BillingState`` set
  can produce (5x5 = 25) through ``billing_state_machine.transition_billing_state``
  and asserts it is permitted exactly when it is on the spec's lifecycle diagram
  and raises otherwise -- not a sample of the diagram's edges, all of them, plus
  every non-edge.
- **``DunningService``'s higher-level methods are the only way the webhook
  handlers and the beat task touch ``billing_state``** -- every test below drives
  those methods, never ``subscription.billing_state = ...`` directly, mirroring
  the contract the phase report has to confirm.

Also carries the two hard constraints inherited from Phases 8/9:
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
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.constants import PaymentProviders
from payments.exceptions import IllegalBillingStateTransitionError
from payments.models import BillingPlan, PaymentMethod, PlanLimit, Subscription
from payments.services.billing_state_machine import (
    LEGAL_BILLING_STATE_TRANSITIONS,
    transition_billing_state,
)
from payments.services.dataclasses import CreatedPlan
from payments.services.dunning_service import DunningService
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
    admin/billing-owner memberships, and a bare ``baker.make(Organization, ...)``
    (unlike ``OrganizationService.create_organization``) creates none on its
    own."""
    return baker.make(
        OrganizationMembership,
        organization=organization,
        user=baker.make(User),
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


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
    *when* the provider is driven, not its wire shape."""

    plan_external_id: str = "ext-plan-1"
    calls: list[str] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)

    def create_subscription_plan(self, plan) -> CreatedPlan:
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

    def change_subscription_plan(self, subscription, new_plan, idempotency_key: str = "") -> None:
        self.calls.append("change_subscription_plan")
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
        """Phase 9 sets ``plan_change_pending_confirmation`` on ``_initiate_upgrade``
        and only clears it on an APPROVED webhook -- a first-upgrade whose charge
        *fails* never reaches that branch. Phase 10 owns the failed-charge path
        and must clear it here, or the org is stuck unable to request a different
        plan (``UnconfirmedPlanChangeError``)."""
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
    def test_grace_to_restricted_notifies(
        self, dunning_service, mock_notification_service, organization, billing_profile
    ):
        _add_admin_membership(organization)
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.GRACE)

        with _patch_on_commit():
            result = dunning_service.expire_grace(subscription)

        assert result.billing_state == BillingState.RESTRICTED
        mock_notification_service.create_notification.assert_called()

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
    def test_active_to_cancelled(self, dunning_service, organization, billing_profile):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)

        result = dunning_service.cancel(subscription)

        assert result.billing_state == BillingState.CANCELLED

    def test_free_to_cancelled_is_not_on_the_diagram(
        self, dunning_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.FREE)

        with pytest.raises(IllegalBillingStateTransitionError):
            dunning_service.cancel(subscription)


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
        # A charge retry was driven -- proof this reached `_process_grace`.
        assert "change_subscription_plan" in fake_payment_service.calls
        assert subscription.last_dunning_attempt_at is not None

    def test_dispatches_restricted_to_the_free_fallback_check_only(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.RESTRICTED)

        dunning_service.process_subscription(subscription)

        # No charge retry for RESTRICTED -- Phase 11 write-blocks it; only the
        # free-fallback check runs.
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

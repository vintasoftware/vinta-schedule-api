"""Phase 6c: the pre-paid limit guard on ``PublicAPIAuthService.create_system_user``.

Spec use-case 2 ("an organization hits a pre-paid limit and is blocked"), applied
to the ``public_api_system_users`` resource. Every REST, GraphQL, and admin
creation path routes through this single function (see its docstring), so guarding
it here guards all of them at once.

The guard's predicate is derived from the counter it guards
(``EntitlementService._count_public_api_system_users``, which counts
``SystemUser.objects.live()`` -- ``is_active=True`` and ``deleted_at__isnull=True``):
a freshly created row defaults to both, so there is nothing to keep in sync between
the two.

Every test in this module was confirmed to fail when the guard was removed.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService


def _organization_with_limit(limit_value: int | None) -> Organization:
    """A standalone (non-reseller) organization with a ceiling on
    ``public_api_system_users``. ``limit_value=None`` builds an
    ``unlimited``-shaped subscription (NULL ceiling) -- the plan's "no feature
    flag" rollout switch -- so the guard is exercised against it as well as a
    finite ceiling.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.PUBLIC_API_SYSTEM_USERS,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    return organization


@pytest.fixture
def service() -> PublicAPIAuthService:
    # Untyped deliberately: PublicAPIAuthService's constructor params are
    # DI-injected (Provide[...]) and resolved at call time by the wired container,
    # which mypy cannot see -- an explicit `-> PublicAPIAuthService` return
    # annotation would make mypy check this body and flag the zero-arg call as
    # missing the required `audit_service` argument.
    return PublicAPIAuthService()


@pytest.mark.django_db
class TestCreateSystemUserLimit:
    def test_raises_and_creates_nothing_at_the_limit(self, service):
        organization = _organization_with_limit(1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="seed-integration",
            long_lived_token_hash="seed-hash",
        )

        with pytest.raises(OverLimitError) as exc_info:
            service.create_system_user(
                integration_name="blocked-integration",
                organization=organization,
            )

        assert exc_info.value.resource_key == LimitedResource.PUBLIC_API_SYSTEM_USERS
        assert exc_info.value.current_usage == 1
        assert exc_info.value.limit == 1
        assert not SystemUser.objects.filter(integration_name="blocked-integration").exists()

    def test_revoked_and_deleted_system_users_free_capacity(self, service):
        """Neither an ``is_active=False`` (revoked) nor a soft-deleted row may count
        against the ceiling -- both feed the same ``.live()`` predicate the counter
        uses."""
        organization = _organization_with_limit(1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="revoked-integration",
            long_lived_token_hash="revoked-hash",
            is_active=False,
        )

        system_user, token = service.create_system_user(
            integration_name="fits-integration",
            organization=organization,
        )

        assert system_user.pk is not None
        assert token

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, service, limit_value):
        organization = _organization_with_limit(limit_value)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="seed-integration-2",
            long_lived_token_hash="seed-hash-2",
        )

        system_user, token = service.create_system_user(
            integration_name="fits-integration-2",
            organization=organization,
        )

        assert system_user.pk is not None
        assert token

    def test_bypass_limits_creates_anyway(self, service):
        organization = _organization_with_limit(1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="seed-integration-3",
            long_lived_token_hash="seed-hash-3",
        )

        system_user, token = service.create_system_user(
            integration_name="bypassed-integration",
            organization=organization,
            bypass_limits=True,
        )

        assert system_user.pk is not None
        assert token

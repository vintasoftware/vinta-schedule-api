"""``payments.seams.permissions.with_working_object_permission`` -- the one
place both of the package's spellings for the billing permission converge
before the object-level defect gets patched. ``MeteredOccurrenceViewSet`` and
``AddOnViewSet`` declare ``IsBillingManager`` as a ``permission_classes``
class attribute; ``SubscriptionViewSet`` and ``BillingProfileViewSet`` build
it per action inside ``get_permissions()``. DRF resolves both spellings to
the same thing -- a list of permission *instances* -- before
``BillingTenantScopedViewMixin.get_permissions`` calls this function, so a
test over that resolved list covers both.

Indirectly covered by ``payments.tests.views.test_billing_views`` (a
child-org admin cannot act on the reseller root), which proves the swapped
permission actually denies. This is the helper's own contract: it swaps
every ``IsBillingManager`` for the fixed subclass and leaves everything else
exactly as it received it.
"""

from rest_framework.permissions import IsAuthenticated
from vinta_billing.permissions import IsBillingManager

from payments.seams.permissions import (
    OrganizationAwareIsBillingManager,
    with_working_object_permission,
)


class TestWithWorkingObjectPermission:
    def test_swaps_is_billing_manager_and_leaves_the_rest_untouched(self):
        is_authenticated = IsAuthenticated()
        is_billing_manager = IsBillingManager()

        result = with_working_object_permission([is_authenticated, is_billing_manager])

        # The non-`IsBillingManager` entry passes through unchanged -- same
        # object, not merely an equal one.
        assert result[0] is is_authenticated
        # `IsBillingManager` is swapped for the subclass with a working
        # object-level check, not merely left as-is or dropped.
        assert isinstance(result[1], OrganizationAwareIsBillingManager)
        assert result[1] is not is_billing_manager

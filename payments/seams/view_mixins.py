"""The host's ``VINTA_BILLING['VIEW_MIXIN']``: tenant scoping, plus one upstream fix.

``vinta_billing.routing`` mixes whatever this setting names in *front* of every
tenant-scoped viewset it mounts, which makes it the seam for both jobs below.

**Tenant scoping** is the reason the setting was pointed anywhere in the first
place: ``common.utils.view_utils.TenantScopedViewMixin`` is what binds this
project's ``X-Organization-Id`` tenancy, and the billing endpoints have to bind
it the same way a host-owned viewset would. That part is inherited unchanged.

**The override below is a workaround for a defect in vinta-django-billing
0.8.0** and should be deleted the moment upstream fixes it.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404

from common.utils.view_utils import TenantScopedViewMixin


class BillingViewMixin(TenantScopedViewMixin):
    """``TenantScopedViewMixin`` plus a corrected billing-profile lookup.

    ``vinta_billing.views.BillingProfileViewSet.get_billing_profile`` reads::

        scope_pk = scope.pk if scope is not None else None
        return get_object_or_404(self.get_queryset(), pk=scope_pk)

    -- it looks the profile up by the **scope's** primary key. That was right up
    to 0.7, where ``BillingProfile.organization`` was ``primary_key=True`` and a
    profile's pk *was* its payer's. 0.8.0 gave ``BillingProfile`` a surrogate
    primary key (its own release notes say so) and did not update this method,
    so the lookup now asks for a profile whose id happens to equal a scope id.

    The two agree only by coincidence. On a fresh database they often do -- both
    sequences start at 1 -- which is why this passes in a short test run and
    fails in a long one. On an **upgraded** database they essentially never do:
    the 0.8 migration carries existing ``BillingProfile`` primary keys across
    untouched (they are the old organization ids) while scopes are created fresh
    and numbered independently. Left unpatched, every read and write of a
    billing profile 404s for effectively every tenant.

    The fix is to drop the ``pk`` filter entirely rather than translate it.
    ``get_queryset()`` already narrows to ``scope=self.request.scope``, and
    ``BillingProfile.scope`` is a ``OneToOneField`` -- so that queryset holds at
    most one row, and the ``pk`` lookup was redundant even when it was correct.

    Defined on the shared mixin because that is the seam this project controls.
    It reaches every billing viewset, but only ``BillingProfileViewSet`` calls
    ``get_billing_profile``, so nothing else sees it.
    """

    def get_billing_profile(self):
        """The active scope's billing profile, or 404."""
        if getattr(self.request, "scope", None) is None:
            # Matches the package's own fail-closed behaviour: `get_queryset`
            # returns `none()` for an unresolved scope, so this 404s rather than
            # serving somebody else's profile.
            return get_object_or_404(self.get_queryset().none())
        return get_object_or_404(self.get_queryset())

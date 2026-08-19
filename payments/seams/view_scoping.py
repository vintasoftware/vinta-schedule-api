"""Teaches ``vinta_billing``'s viewsets how *this* project selects an organization.

``vinta_billing.view_mixins.TenantScopedViewMixin`` reads an organization that
something else already resolved: ``vinta-django-orgs``' middleware, or the
context a background job bound. This project runs neither on the DRF surface --
``MIDDLEWARE`` in ``settings/base.py`` carries no organization middleware -- and
resolves in the view layer instead, from the ``X-Organization-Id`` header, with
its own 400 for an ambiguous caller and 403 for a non-member. That contract is
``common.utils.view_utils.TenantScopedViewMixin``'s, and it is the host's, not a
billing library's: the header name, the refusal bodies and the resolution table
are this API's.

Mounting the package's viewsets bare therefore resolves no organization at all,
and every billing endpoint answers 403 ("An active organization is required to
manage billing") or an empty page. Mixing the host's mixin in front of them is
what closes that, and :class:`BillingTenantScopedViewMixin` is the one method of
glue that takes.
"""

from __future__ import annotations

from django.db.models import Model

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from common.utils.view_utils import TenantScopedViewMixin
from payments.seams.permissions import with_working_object_permission


class BillingTenantScopedViewMixin(TenantScopedViewMixin):
    """The host's ``X-Organization-Id`` resolution, in front of a package viewset.

    Both mixins in the resulting MRO define ``resolve_organization``, and they
    mean different things by it:

    - the host's (``vinta_orgs.drf.OrganizationScopedAPIViewMixin``) *assigns*
      ``request.organization`` as a side effect and returns ``None``;
    - the package's *returns* the organization, and its ``initial()`` assigns
      whatever came back.

    The host's wins on name resolution, so without this override the package's
    ``initial()`` would assign that ``None`` straight over the organization
    ``perform_authentication`` had just resolved -- and every endpoint would
    403. This method makes the one name satisfy both contracts: it still
    performs the assignment its own caller expects, and it also returns what the
    package's caller expects. Everything downstream of the assignment -- the
    package's ``get_organization``, its ``filter_queryset_by_organization``, and
    ``IsBillingManager`` -- reads ``request.organization`` and needs nothing
    further from here.

    The ``hasattr`` guard is not an optimisation. The two callers run in the
    same request -- ``perform_authentication`` first, from inside
    ``APIView.initial``, then the package's ``initial()`` once that returns --
    and re-resolving would re-run the membership query, and re-raise the 400 or
    403 for a caller who is meant to have been refused exactly once.
    """

    def resolve_organization(self, request: Request) -> Model | None:  # type: ignore[override]
        if hasattr(request, "organization"):
            return request.organization
        super().resolve_organization(request)
        return getattr(request, "organization", None)

    def get_permissions(self) -> list[BasePermission]:
        """Repair the package's object-level billing check on the way past.

        Temporary, and not this mixin's real job -- see
        ``payments.seams.permissions``, which explains the 0.4.0 defect and is
        the module to delete once it is fixed upstream. It is applied here
        because this mixin is the one thing every tenant-scoped billing viewset
        already carries.
        """
        return with_working_object_permission(super().get_permissions())

"""Organization retrievers for vinta-django-orgs' ``ORGANIZATION_RETRIEVERS`` setting.

**Phase 1b of the vinta-django-orgs migration** (see
``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``). This
module ships tested but **unregistered** — ``SHARED_SCHEMA_ORGANIZATIONS`` (the
setting whose ``ORGANIZATION_RETRIEVERS`` list this retriever plugs into) is
created in Phase 1c together with the package's Django app installation and
the ``ORGANIZATION_MODEL`` / ``ORGANIZATION_MEMBERSHIP_MODEL`` swappable
settings. Nothing in this repo calls ``retrieve_by_x_organization_id`` yet;
``TenantScopedViewMixin`` starts consulting it in Phase 2b.

The signature deliberately matches the package's own retrievers
(``organizations.organization_retrievers.retrieve_by_domain`` /
``retrieve_by_http_header`` / ``retrieve_by_session``, all
``(request: HttpRequest) -> Organization | None``) so Phase 1c can register it
in ``ORGANIZATION_RETRIEVERS`` unchanged.
"""

from __future__ import annotations

from django.http import HttpRequest

from tenancy.models import Organization


#: Header name used to select the active organization for a request. Mirrors
#: ``common.utils.view_utils.ACTIVE_ORG_HEADER`` — kept as a separate constant
#: here (rather than imported) so this module has no dependency on
#: ``common.utils.view_utils``, which is DRF-specific and imports
#: ``rest_framework``; this retriever must stay importable from plain Django
#: request-handling code with no DRF in the import chain.
ORGANIZATION_ID_HEADER = "X-Organization-Id"


def retrieve_by_x_organization_id(request: HttpRequest) -> Organization | None:
    """Resolve the active organization from the ``X-Organization-Id`` header.

    Reads the header as an integer primary key and looks the organization up
    directly — this retriever does not consult membership, so it makes no
    statement about whether the requesting user may act as that organization;
    it only resolves *which* organization the header names.

    Returns ``None`` — never raises — for:
    - a missing or empty header,
    - a header value that is not a valid integer,
    - a header naming an organization id that does not exist.

    Args:
        request: The incoming Django ``HttpRequest``.

    Returns:
        The ``Organization`` the header names, or ``None``.
    """
    header_value = request.headers.get(ORGANIZATION_ID_HEADER)
    if not header_value:
        return None

    try:
        organization_id = int(header_value)
    except (TypeError, ValueError):
        return None

    return Organization.objects.filter(pk=organization_id).first()

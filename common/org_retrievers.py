"""``ORGANIZATION_RETRIEVERS`` entry that reads this project's own header.

``vinta-django-orgs`` resolves the acting organization by calling each dotted
path in ``SHARED_SCHEMA_ORGANIZATIONS['ORGANIZATION_RETRIEVERS']`` with the
request until one returns an organization. None of the three retrievers it
ships fits us: ``retrieve_by_domain`` is subdomain tenancy (which this project
deliberately does not do),
``retrieve_by_session`` reads the session (we are token-authenticated), and
``retrieve_by_http_header`` reads an ``Organization-Slug`` header by *slug*
where our long-standing wire contract is ``X-Organization-Id`` by integer
primary key.

**This returns ``None`` for an unknown id, where the package's
``retrieve_by_http_header`` raises ``OrganizationNotFoundError``. That is
deliberate, not an oversight.** ``common.utils.view_utils.
TenantScopedViewMixin`` owns the response for "the header names an
organization you are not a member of" (403) and for "the header is required"
(400), and it can only do that because it sees the caller's memberships --
which a retriever does not. A retriever that raised would turn a 403 into a
500 on the paths the mixin has opted out of. The package's own middleware,
which is what would surface that exception, is deliberately not installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpRequest

from common.constants import ACTIVE_ORG_HEADER


if TYPE_CHECKING:
    from organizations.models import Organization


def retrieve_by_x_organization_id(request: HttpRequest) -> Organization | None:
    """Resolve the organization named by the ``X-Organization-Id`` header.

    Returns ``None`` -- never raises -- when the header is missing, empty, not
    an integer, or names an organization that does not exist.

    ``Organization`` is the tenant root, not tenant-scoped data: it carries no
    ``SingleOrganizationModelMixin``, so ``objects`` here is Django's stock
    manager and this lookup neither needs nor bypasses an organization filter.
    """
    # Deferred: settings are imported long before the app registry is ready, and
    # this module is named from a settings value.
    from organizations.models import Organization

    raw_value = request.headers.get(ACTIVE_ORG_HEADER)
    if not raw_value:
        return None

    try:
        organization_id = int(raw_value)
    except (TypeError, ValueError):
        return None

    return Organization.objects.filter(pk=organization_id).first()

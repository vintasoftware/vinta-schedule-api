"""Who may manage billing here, who hears about it, and whose billing a request acts on.

``vinta_billing.contrib.orgs`` ships all three, and this project used them until
``BILLING_SCOPE_MODEL`` moved to ``billing_integration.OrganizationBillingScope``.
Every function there reads the payer off the shipped scope's generic key
(``scope.object_id``), which this project's scope does not have -- it holds a
real ``organization`` foreign key instead. The package says as much in that
module's docstring: a project that swaps the scope model copies these and reads
its own field.

They fail *quietly* if left pointing at the package, which is why they are here
rather than left to be noticed later. ``organization_pk_for`` returns ``None``
for a scope with no ``object_id``, so the predicate refuses everybody (403 on
every billing endpoint), the recipient list is empty (a dunning ladder that
tells nobody, ending in a suspension the payer was never warned about), and the
resolver answers ``None`` (404 on every billing read). None of the three raises.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vinta_billing.permissions import MANAGE_BILLING_PERMISSION


def member_holding_manage_billing(user: Any, scope: Any) -> bool:
    """A member of the scope's organization holding ``vinta_billing.manage_billing``.

    Asks ``vinta-django-orgs``' organization-scoped question, not
    ``user.has_perm``: the latter answers for whichever organization is *bound*,
    unions in global permissions and groups, and says yes to every superuser.
    Billing is routinely read against a reseller **root** that is an ancestor of
    the bound organization, so all three would answer a question nobody asked.
    """
    from vinta_orgs.authorization import has_organization_permission

    organization = getattr(scope, "organization", None)
    if organization is None:
        return False
    return bool(has_organization_permission(user, MANAGE_BILLING_PERMISSION, organization))


def members_holding_manage_billing(scope: Any) -> Sequence[Any]:
    """The members who hold ``manage_billing`` in the scope's organization.

    The counterpart to :func:`member_holding_manage_billing`, so "who may change
    billing" and "who is told when it goes wrong" come from one grant rather
    than drifting apart.

    Inactive memberships are excluded: a deactivated member is not somebody to
    tell, and ``holding_permission`` alone does not exclude them.
    """
    from vinta_orgs.conf import get_organization_membership_model

    organization_id = getattr(scope, "organization_id", None)
    if organization_id is None:
        return []
    return list(
        get_organization_membership_model()
        .objects.filter(organization_id=organization_id)
        .active()
        .holding_permission(MANAGE_BILLING_PERMISSION)
        .values_list("user_id", flat=True)
        .distinct()
    )


def resolve_scope_from_organization(request: Any) -> Any | None:
    """``SCOPE_RESOLVER``: the scope billing the organization this request resolved.

    Bridges the ``request.organization`` this project already resolves off
    ``X-Organization-Id`` (``common.utils.view_utils.TenantScopedViewMixin``) to
    the scope that bills it.

    Does not create one: resolving a request must not write. Provisioning is
    ``payments.seams.scopes``' job, off ``post_save``, so by the time a request
    names an organization its scope exists.

    Takes whatever already set ``request.scope`` first, so a caller that
    resolved tenancy some other way is not overridden.
    """
    from vinta_billing.conf import get_scope_model

    scope = getattr(request, "scope", None)
    if scope is not None:
        return scope

    organization = getattr(request, "organization", None)
    if organization is None:
        return None
    return get_scope_model().objects.filter(organization=organization).first()

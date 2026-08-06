from typing import Literal

from calendar_integration.models import Calendar
from calendar_integration.querysets import CalendarGroupQuerySet
from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser


SystemUserScope = Literal["org_wide", "scoped_admin", "scoped_member"]


def _resolve_scoped_membership(
    system_user: SystemUser, organization: Organization
) -> OrganizationMembership | None:
    """Resolve the active ``OrganizationMembership`` a scoped token acts as.

    Returns ``None`` when the token is org-wide (``scoped_to_membership_user_id``
    is ``None``) or when the scoped membership is missing/inactive -- both
    cases the caller must treat as "no membership to elevate/act through",
    never silently falling back to unrestricted access.
    """
    if system_user.scoped_to_membership_user_id is None:
        return None
    return OrganizationMembership.objects.filter(
        organization_id=organization.id,
        user_id=system_user.scoped_to_membership_user_id,
        is_active=True,
    ).first()


def _resolve_scope_and_membership(
    system_user: SystemUser, organization: Organization
) -> tuple[SystemUserScope, OrganizationMembership | None]:
    """Resolve the scope label and active membership for a system user in one call.

    Combines ``system_user_scope`` and ``_resolve_scoped_membership`` logic
    to avoid redundant database queries when both the scope and membership
    are needed (common in filtering operations).

    Returns a tuple of (scope, membership):
    - scope: "org_wide" if token is unrestricted, "scoped_admin" if an
      active admin member, "scoped_member" otherwise (including when the
      membership is missing/inactive, fail-closed).
    - membership: The active OrganizationMembership if scoped, or None if
      org-wide or missing/inactive.
    """
    if system_user.scoped_to_membership_user_id is None:
        return ("org_wide", None)
    membership = _resolve_scoped_membership(system_user, organization)
    if membership is not None and membership.is_admin:
        return ("scoped_admin", membership)
    return ("scoped_member", membership)


def system_user_scope(system_user: SystemUser, organization: Organization) -> SystemUserScope:
    """Resolve a ``SystemUser`` token's effective role-based scope for `organization`.

    - ``"org_wide"``: ``scoped_to_membership_user_id`` is ``None`` -- unrestricted,
      matches today's default token behavior exactly (unchanged by this).
    - ``"scoped_admin"``: the token is scoped to an active membership whose role
      is ADMIN -- acts with that membership's admin powers (all calendar groups,
      any calendar).
    - ``"scoped_member"``: the token is scoped to a membership that is either
      not an admin, or missing/inactive. A missing/inactive scoped membership
      is deliberately collapsed into this same value with EMPTY access
      (fail closed) rather than a separate "unknown" state -- see
      ``scoped_calendar_ids`` and ``scoped_calendar_group_queryset``, which
      both re-resolve the membership to distinguish "real member" from
      "missing/inactive" and return empty for the latter.
    """
    scope, _ = _resolve_scope_and_membership(system_user, organization)
    return scope


def scoped_calendar_ids(system_user: SystemUser, organization: Organization) -> set[int] | None:
    """Return the set of calendar IDs this token may access, or None if unrestricted.

    None => unrestricted (org-wide OR scoped-admin token). A set (possibly
    empty) => the only calendar ids this token may touch.

    Args:
        system_user: The SystemUser (token) making the request.
        organization: The organization context.

    Returns:
        None if the token is org-wide (``scoped_to_membership_user_id`` is
        ``None``) or scoped to an active admin membership (``scoped_admin``
        -- a scoped-admin token gets the same unrestricted powers an org-wide
        token has for calendar-group operations); a set of calendar IDs
        (empty when the scoped membership is missing/inactive -- fail
        closed, never elevate) otherwise.
    """
    scope, membership = _resolve_scope_and_membership(system_user, organization)
    if scope == "org_wide" or scope == "scoped_admin":
        return None
    if membership is None:
        # Scoped token whose membership was revoked/deactivated since minting:
        # fail closed with no access, rather than falling back to unrestricted.
        return set()
    # The queryset is already org-scoped via filter_by_organization, so filtering
    # ownerships by the membership's user_id is equivalent to the full
    # (organization_id, user_id) membership identity — the org is fixed.
    return set(
        Calendar.objects.filter_by_organization(organization.id)
        .filter(ownerships__membership_user_id=system_user.scoped_to_membership_user_id)
        .distinct()
        .values_list("id", flat=True)
    )


def scoped_calendar_group_queryset(
    system_user: SystemUser | None,
    organization: Organization,
    base_qs: CalendarGroupQuerySet,
) -> CalendarGroupQuerySet:
    """Apply role-aware ``CalendarGroup`` visibility scoping to `base_qs`.

    - ``system_user`` is ``None`` (non-public-API / internal path): no-op,
      returns `base_qs` unchanged.
    - ``org_wide`` / ``scoped_admin``: no-op, returns `base_qs` unchanged --
      unrestricted, sees every group in the organization.
    - ``scoped_member`` with an active resolved membership: filtered to the
      groups that membership participates in
      (``CalendarGroupQuerySet.only_member_of``).
    - ``scoped_member`` whose membership is missing/inactive: empty (fail
      closed) -- a revoked/deactivated scoped token must not fall back to
      seeing every group in the org.

    Args:
        system_user: The SystemUser (token) making the request, or None.
        organization: The organization context.
        base_qs: An already organization-filtered ``CalendarGroupQuerySet``.

    Returns:
        The (possibly further-filtered) ``CalendarGroupQuerySet``.
    """
    if system_user is None:
        return base_qs
    scope, membership = _resolve_scope_and_membership(system_user, organization)
    if scope != "scoped_member":
        return base_qs
    if membership is None:
        return base_qs.none()
    return base_qs.only_member_of(membership.user_id)


def assert_calendar_in_owner_scope(
    system_user: SystemUser | None,
    organization: Organization,
    calendar_id: int,
) -> None:
    """Assert that the given calendar_id is accessible by the token's owner scope.

    This is the shared write-side guard reused by all owner-guarded mutations.
    It is a no-op when the system_user is None or when the token is org-wide
    (scoped_calendar_ids returns None). When the token is scoped and calendar_id
    is NOT in the allowed set, raises Calendar.DoesNotExist with the same message
    that a genuinely-missing calendar produces — the caller must not reveal whether
    the target exists.

    Args:
        system_user: The SystemUser (token) making the request, or None (no-op).
        organization: The organization context.
        calendar_id: The calendar ID targeted by the write operation.

    Raises:
        Calendar.DoesNotExist: When the token is scoped and calendar_id is outside
            the owner's allowed calendar set. The message is intentionally identical
            to a real not-found error to prevent existence leaks.
    """
    if system_user is None:
        return
    allowed_ids = scoped_calendar_ids(system_user, organization)
    if allowed_ids is not None and calendar_id not in allowed_ids:
        raise Calendar.DoesNotExist("Calendar matching query does not exist.")

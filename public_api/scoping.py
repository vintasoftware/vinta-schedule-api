from calendar_integration.models import Calendar
from organizations.models import Organization
from public_api.models import SystemUser


def scoped_calendar_ids(system_user: SystemUser, organization: Organization) -> set[int] | None:
    """Return the set of calendar IDs this token may access, or None if unrestricted.

    None => unrestricted (org-wide token). A set (possibly empty) => the
    only calendar ids this token may touch.

    Args:
        system_user: The SystemUser (token) making the request.
        organization: The organization context.

    Returns:
        None if the token is org-wide (scoped_to_membership_fk is None);
        a set of calendar IDs (possibly empty) if scoped to a membership.
    """
    if system_user.scoped_to_membership_fk_id is None:
        return None
    return set(
        Calendar.objects.filter_by_organization(organization.id)
        .filter(ownerships__user__organization_memberships=system_user.scoped_to_membership_fk_id)
        .distinct()
        .values_list("id", flat=True)
    )

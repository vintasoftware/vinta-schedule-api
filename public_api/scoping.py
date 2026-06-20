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
        .filter(ownerships__membership__id=system_user.scoped_to_membership_fk_id)
        .distinct()
        .values_list("id", flat=True)
    )


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

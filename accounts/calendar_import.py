"""Queues the import of a user's external calendars once a social account is stored.

Two places store a social account: ``SocialAccountAdapter.save_user`` on social
signup, and the ``social_account_added`` receiver in ``accounts.signals`` when an
account is linked to an existing user. Both call ``request_calendar_import``.
"""

from django.db import transaction

from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import CalendarProvider
from calendar_integration.tasks import import_account_calendars_task
from common.organization_services import memberships
from organizations.models import OrganizationMembership
from users.models import User


def request_calendar_import(
    user: User,
    account: SocialAccount,
    membership: OrganizationMembership | None = None,
) -> None:
    """Queue an import of the external calendars reachable through ``account``.

    Runs on social signup (``SocialAccountAdapter.save_user``) and when a social
    account is linked to an existing user (``accounts.signals``).

    No service account is required: a user's own calendars import through their
    OAuth social-account token (account_type="social_account"). The
    GoogleCalendarServiceAccount path is only for org-wide room/resource imports.

    Only Google and Microsoft accounts have calendars, so other providers are
    skipped. Calendars are imported into an organization, so the user needs an
    active membership. A user with no membership yet (for example, an uninvited
    social signup) is skipped here. Their import runs later, when they call the
    request-import endpoint after joining an organization.
    """
    if account.provider not in (CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT):
        return

    # On signup, `membership` is the organization the user was just invited to.
    # Use it, so a user who joins a second organization gets the import there.
    # Otherwise look the membership up. The lookup is non-strict: a user with
    # several memberships returns None and is skipped, because picking one by
    # creation order would be a guess.
    account_id = account.id
    organization_id = membership.organization_id if membership is not None else None
    if organization_id is None:
        resolved_membership = memberships.resolve_for_user(user, strict=False)
        if resolved_membership is None:
            return
        organization_id = resolved_membership.organization_id
    # `robust=True`: the import is optional, so a failed `.delay()` (e.g. the broker
    # is down) must not fail the login. The provider callback runs outside a
    # transaction, where Django calls this immediately, and the social account is
    # already committed by then. Django logs the exception with its traceback.
    transaction.on_commit(
        lambda: import_account_calendars_task.delay(
            account_type="social_account",
            account_id=account_id,
            organization_id=organization_id,
        ),
        robust=True,
    )

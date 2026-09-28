"""Imports a user's calendars when a social account is linked to an existing user.

allauth creates a new user through ``SocialAccountAdapter.save_user``, and that is
where a social signup queues its calendar import. Linking a social account to a
user who already exists never reaches ``save_user``: it goes through
``SocialLogin.connect``, which only sends ``social_account_added``. That happens
when a Google login matches an existing account by email (see
``SocialAccountAdapter.can_authenticate_by_email``) and when a signed-in user
connects a provider. Without this receiver the account and its OAuth token are
stored, but the user's calendars are never imported.
"""

from typing import Any

from django.dispatch import receiver
from django.http import HttpRequest

from allauth.socialaccount.models import SocialLogin
from allauth.socialaccount.signals import social_account_added

from accounts.calendar_import import request_calendar_import


@receiver(social_account_added, dispatch_uid="accounts.signals.import_linked_account_calendars")
def import_linked_account_calendars(
    sender: type[SocialLogin],
    request: HttpRequest,
    sociallogin: SocialLogin,
    **kwargs: Any,
) -> None:
    """Queue the calendar import for a social account just linked to an existing user."""
    request_calendar_import(sociallogin.user, sociallogin.account)

"""
Signal handlers for the accounts app.

Phase 3 — Create own org on email verification (no invite):
On email confirmation, provision a tenant for the user using
OrganizationService.provision_tenant_for_user. This fires for both
email-link and code-based (headless) verification paths because allauth's
verify_email() always emits the email_confirmed signal regardless of
verification method.
"""

import logging

from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed

from organizations.exceptions import UserAlreadyHasMembershipError
from users.models import Profile


logger = logging.getLogger(__name__)


def _provision_on_email_confirmed(
    sender: type[EmailAddress],
    request,
    email_address: EmailAddress,
    **kwargs,
) -> None:
    """
    Handle the allauth ``email_confirmed`` signal.

    Resolves OrganizationService from the DI container and calls
    provision_tenant_for_user so the user is bound to an org (or a pending
    invite is auto-accepted) the moment their email is verified.

    Idempotent: if the user already has a membership (re-confirmation, or a
    race against another path), the UserAlreadyHasMembershipError is swallowed
    as a deliberate no-op.
    """
    from di_core.containers import container as di_container

    user = email_address.user
    if user is None:
        logger.warning(
            "email_confirmed signal received with no associated user "
            "(email=%s); skipping provisioning.",
            email_address.email,
        )
        return

    # Guard: be robust if the profile is absent (shouldn't happen in normal flow)
    try:
        profile = user.profile
    except Profile.DoesNotExist:
        logger.warning(
            "User %s has no profile at email confirmation time; "
            "skipping provisioning and leaving user membership-less.",
            user.pk,
        )
        return

    organization_name = profile.pending_organization_name or None

    if di_container is None:
        logger.error(
            "DI container is not initialised; cannot provision tenant for user %s.",
            user.pk,
        )
        return

    organization_service = di_container.organization_service()

    try:
        membership = organization_service.provision_tenant_for_user(
            user=user,
            organization_name=organization_name,
        )
    except UserAlreadyHasMembershipError:
        # Idempotent: re-confirmation or race — user already has a membership.
        logger.debug(
            "User %s already has a membership; skipping re-provisioning.",
            user.pk,
        )
        return

    if membership is not None:
        # Clear the stashed org name now that provisioning succeeded.
        # Only write if there is actually something to clear (avoids a
        # redundant "" -> "" save on the invite path where Phase 2 already
        # left the field blank).
        if profile.pending_organization_name:
            profile.pending_organization_name = ""
            profile.save(update_fields=["pending_organization_name"])
        logger.info(
            "Provisioned tenant for user %s (membership=%s, org=%s).",
            user.pk,
            membership.pk,
            membership.organization_id,
        )
    else:
        # No pending invite, no org name → user stays gated (onboarding later).
        logger.debug(
            "No pending invite and no org name for user %s; user remains membership-less (gated).",
            user.pk,
        )


def connect() -> None:
    """Connect all signal handlers for the accounts app."""
    email_confirmed.connect(
        _provision_on_email_confirmed,
        sender=EmailAddress,
        dispatch_uid="accounts.signals.provision_on_email_confirmed",
    )

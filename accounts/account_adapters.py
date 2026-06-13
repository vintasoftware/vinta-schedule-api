import datetime
import logging
from copy import deepcopy
from typing import Annotated

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.headless.adapter import DefaultHeadlessAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.utils import build_absolute_uri
from dependency_injector.wiring import Provide, inject
from vintasend.constants import NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService

from organizations.exceptions import UserAlreadyHasMembershipError
from organizations.models import get_active_organization_membership
from organizations.services import OrganizationService
from users.models import Profile, User


logger = logging.getLogger(__name__)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        self.organization_service = organization_service
        super().__init__(*args, **kwargs)

    def get_connect_redirect_url(self, request, socialaccount):
        return reverse("index")

    @staticmethod
    def _get_profile(user: User) -> Profile | None:
        """Return the user's Profile (persisted or in-memory), or None.

        Accessing ``user.profile`` raises ``RelatedObjectDoesNotExist`` for a
        user with no profile (e.g. the unsaved User built during a pending
        social signup), so callers that must tolerate its absence go through
        here.
        """
        try:
            return user.profile
        except Profile.DoesNotExist:
            return None

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        # The User model carries no name fields — they live on Profile — so the
        # default populate_user drops the provider's first/last name. Attach an
        # in-memory Profile populated from the provider so the names survive the
        # signup round-trip (serialize/deserialize prefilling the form) and the
        # formless auto-signup path. Don't clobber an existing profile (connect
        # flow reuses an already-registered user).
        if self._get_profile(user) is None:
            user.profile = Profile(
                user=user,
                first_name=data.get("first_name") or "",
                last_name=data.get("last_name") or "",
            )
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form=form)
        # Auto-signup has no signup form, and the default save_user never
        # creates a Profile, so a socially-created user would otherwise have
        # none. Guarantee the User.profile invariant here. get_or_create only
        # applies the provider names on creation, so a user-edited signup form
        # (which creates the Profile itself) is never overwritten.
        pending = self._get_profile(user)
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={
                "first_name": pending.first_name if pending else "",
                "last_name": pending.last_name if pending else "",
            },
        )
        user.profile = profile
        self._enqueue_profile_picture_download(profile, sociallogin)
        # Phase 6 — Auto-join invited org on social signup.
        # At this point the user is persisted (has a pk) and the Profile
        # invariant is satisfied.  Attempt to provision a tenant: if a
        # pending invitation matches the social email the user becomes a MEMBER
        # of the inviting org immediately, skipping the create-org prompt.
        # When there is no matching invitation, provision_tenant_for_user
        # returns None and the user stays membership-less (gated), preserving
        # the Phase 5 uninvited-stays-gated behavior.
        # UserAlreadyHasMembershipError is treated as a no-op so that a social
        # re-login for a user who is already a member does not raise.
        self._provision_org_membership(user)
        self._request_calendar_import(user, sociallogin)
        return user

    def _request_calendar_import(self, user: User, sociallogin: SocialLogin) -> None:
        """Trigger an import of the user's external calendars on social signup.

        No service account is required: a user's own calendars import through
        their OAuth social-account token (account_type="social_account"). The
        GoogleCalendarServiceAccount path is only for org-wide room/resource
        imports.

        Only Google/Microsoft accounts carry calendars; other providers are
        ignored. The import is org-scoped, so it requires an active membership —
        when the user is still gated (no membership, e.g. an uninvited social
        signup) the import is skipped here and runs later when they hit the
        request-import endpoint after provisioning.
        """
        from calendar_integration.constants import CalendarProvider
        from calendar_integration.tasks import import_account_calendars_task

        account = getattr(sociallogin, "account", None)
        if account is None or account.provider not in (
            CalendarProvider.GOOGLE,
            CalendarProvider.MICROSOFT,
        ):
            return

        membership = get_active_organization_membership(user)
        if membership is None:
            return

        account_id = account.id
        organization_id = membership.organization_id
        transaction.on_commit(
            lambda: import_account_calendars_task.delay(
                account_type="social_account",
                account_id=account_id,
                organization_id=organization_id,
            )
        )

    def _provision_org_membership(self, user: User) -> None:
        """Attempt to auto-join the user to an inviting organisation.

        Uses the DI-injected OrganizationService (supplied via ``__init__``,
        mirroring AccountAdapter) and calls provision_tenant_for_user with no
        organisation_name — social signups never carry one.  No-ops silently on
        UserAlreadyHasMembershipError (re-login or race).
        """
        if not user.email:
            logger.warning(
                "Social user %s has no email; skipping org provisioning.",
                user.pk,
            )
            return

        try:
            membership = self.organization_service.provision_tenant_for_user(user=user)
        except UserAlreadyHasMembershipError:
            # Re-login or race: user already has a membership — no-op.
            logger.debug(
                "Social user %s already has a membership; skipping re-provisioning.",
                user.pk,
            )
            return

        if membership is not None:
            logger.info(
                "Social user %s auto-joined org %s as MEMBER (membership=%s).",
                user.pk,
                membership.organization_id,
                membership.pk,
            )
        else:
            logger.debug(
                "No pending invite for social user %s; user remains membership-less (gated).",
                user.pk,
            )

    @staticmethod
    def _enqueue_profile_picture_download(profile: Profile, sociallogin) -> None:
        """Pull the provider avatar into S3 asynchronously, if any and not set."""
        from users.tasks import download_social_profile_picture

        if profile.profile_picture:
            return
        account = getattr(sociallogin, "account", None)
        extra_data = getattr(account, "extra_data", None) or {}
        picture_url = extra_data.get("picture")
        if picture_url:
            download_social_profile_picture.delay(profile.pk, picture_url)

    def serialize_instance(self, instance):
        if isinstance(instance, SocialLogin):
            serialized_social_login = super().serialize_instance(instance)
            serialized_social_login["user"] = self.serialize_instance(instance.user)
            return serialized_social_login

        if isinstance(instance, User):
            # Tolerate a missing profile (the unsaved User built during a pending
            # social signup has none yet); mirrors deserialize_instance, which
            # rebuilds an in-memory Profile on the way back.
            profile = self._get_profile(instance)
            # If the instance is a User, use the UserSerializer to serialize it
            return {
                "id": instance.id,
                "email": instance.email,
                "phone_number": instance.phone_number,
                "is_active": instance.is_active,
                "is_staff": instance.is_staff,
                "is_superuser": instance.is_superuser,
                "created": instance.created.isoformat()
                if isinstance(instance.created, datetime.datetime)
                else instance.created,
                "modified": instance.modified.isoformat()
                if isinstance(instance.modified, datetime.datetime)
                else instance.modified,
                "last_login": instance.last_login.isoformat()
                if isinstance(instance.last_login, datetime.datetime)
                else instance.last_login,
                "profile": {
                    "first_name": profile.first_name if profile else "",
                    "last_name": profile.last_name if profile else "",
                    "profile_picture": profile.profile_picture.url
                    if profile and profile.profile_picture
                    else None,
                },
            }
        return super().serialize_instance(instance)

    def deserialize_instance(self, model, data):
        if model == SocialLogin:
            # If the model is SocialLogin, use the SocialLoginSerializer to deserialize it
            data_copy = deepcopy(data)
            user_data = data_copy.pop("user", {})
            social_login = SocialLogin.deserialize(data_copy)
            social_login.user = self.deserialize_instance(User, user_data)
            return social_login

        if model == User:
            data_copy = deepcopy(data)
            # If the model is User, use the UserSerializer to deserialize it
            if data_copy.get("id"):
                return User.objects.filter(id=data_copy["id"]).first()

            profile_data = data_copy.pop("profile", {})
            user = User(**data_copy)
            user.profile = Profile(**profile_data, user=user)
            return user
        return super().deserialize_instance(model, data)


@inject
class AccountAdapter(DefaultAccountAdapter):
    def __init__(
        self,
        *args,
        notification_service: Annotated[NotificationService, Provide["notification_service"]],
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        self.notification_service = notification_service
        self.organization_service = organization_service
        super().__init__(*args, **kwargs)

    def send_password_reset_mail(self, user, email, context):
        """
        Sends a password reset email to the user.
        """
        super().send_password_reset_mail(user, email, context)
        self.notification_service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.EMAIL.value,
            title="Password Reset Request",
            body_template="accounts/notifications/emails/password_reset_request.body.html",
            subject_template="accounts/notifications/emails/password_reset_request.subject.txt",
            preheader_template="accounts/notifications/emails/password_reset_request.pre_header.txt",
            context_name="password_reset_context",
            context_kwargs=NotificationContextDict(
                {
                    "user_id": user.id,
                    "password_reset_url": build_absolute_uri(
                        reverse("account_reset_password_from_key", args=[context["key"]])
                    ),
                }
            ),
        )

    def send_confirmation_mail(self, request, emailconfirmation, signup):
        """
        Sends a confirmation email to the user.
        """
        from allauth.account import app_settings

        ctx = {
            "user_id": emailconfirmation.email_address.user_id,
        }
        if app_settings.EMAIL_VERIFICATION_BY_CODE_ENABLED:
            ctx.update({"code": emailconfirmation.key})
        else:
            ctx.update(
                {
                    "key": emailconfirmation.key,
                    "activate_url": self.get_email_confirmation_url(request, emailconfirmation),
                }
            )
        if signup:
            email_template = "accounts/notifications/emails/confirmation_signup"
        else:
            email_template = "accounts/notifications/emails/confirmation"

        self.notification_service.create_notification(
            user_id=emailconfirmation.email_address.user_id,
            notification_type=NotificationTypes.EMAIL.value,
            title="Email Confirmation",
            body_template=f"{email_template}.body.html",
            subject_template=f"{email_template}.subject.txt",
            preheader_template=f"{email_template}.pre_header.txt",
            context_name="email_confirmation_context",
            context_kwargs=NotificationContextDict(ctx),
        )

    def confirm_email(self, request, email_address: EmailAddress) -> bool:
        """Override to provision a tenant the moment the user's email is verified.

        Calls the default allauth confirm_email (which marks the address verified and
        emits the email_confirmed signal) then, on success, delegates to the
        DI-injected OrganizationService's provision_tenant_for_user. This provisions
        the tenant imperatively in the adapter — at the moment the email is verified —
        rather than via a decoupled email_confirmed signal handler, so the email/password
        path's provisioning lives at an explicit, step-debuggable call site (mirroring the
        intent of provisioning inside the adapters rather than through signals).

        OrganizationService arrives via constructor injection (see ``__init__``),
        matching how ``notification_service`` is supplied, instead of reaching into the
        DI container global at the call site.

        Idempotent: swallows UserAlreadyHasMembershipError so re-confirmation is a no-op.
        """
        confirmed = super().confirm_email(request, email_address)
        if not confirmed:
            return confirmed

        user = email_address.user
        try:
            profile = user.profile
        except Profile.DoesNotExist:
            logger.warning(
                "User %s has no profile at email confirmation time; "
                "skipping provisioning and leaving user membership-less.",
                user.pk,
            )
            return confirmed

        organization_name = profile.pending_organization_name or None

        try:
            membership = self.organization_service.provision_tenant_for_user(
                user=user,
                organization_name=organization_name,
            )
        except UserAlreadyHasMembershipError:
            # Idempotent: re-confirmation or race — user already has a membership.
            logger.debug(
                "User %s already has a membership; skipping re-provisioning.",
                user.pk,
            )
            return confirmed

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

        return confirmed

    def send_mail(self, template_prefix, email, context):
        msg = super().render_mail(template_prefix, email, context)
        msg.extra_headers = {"X-SES-CONFIGURATION-SET": settings.SES_CONFIGURATION_SET}
        msg.send()

    def get_phone(self, user: User) -> tuple[str, bool] | None:
        """
        Retrieves the phone number for the given user.
        Returns a tuple of the phone number and whether it is verified.
        If no phone number is found, returns None.
        """
        return user.phone_number, user.phone_verified_date is not None

    def set_phone(self, user: User, phone: str, verified: bool = False):
        """
        Sets the phone number (and verified status) for the given user.
        If the user already has a phone number, it will be updated.
        """
        user.phone_number = phone
        user.phone_verified_date = datetime.datetime.now(tz=datetime.UTC) if verified else None
        user.save()

    def set_phone_verified(self, user: User, phone: str):
        return self.set_phone(user, phone, verified=True)

    def get_user_by_phone(self, phone: str):
        """
        Retrieves a user by their phone number.
        Returns the user object if found, otherwise returns None.
        """
        try:
            return User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return None

    def send_verification_code_sms(self, user, phone: str, code: str, **kwargs):
        """
        Sends a verification code.
        """
        if not user:
            logger.warning("No user provided for sending verification code SMS.")
            return

        if not phone:
            logger.warning("No phone number provided for sending verification code SMS.")
            return

        # Here you would implement the logic to send the SMS.
        # This is a placeholder for the actual SMS sending logic.
        logger.info(
            "Sending verification code %s to %s for user %s.",
            code,
            phone,
            user.id,
        )
        self.notification_service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.SMS.value,
            title="Phone Verification Message",
            body_template="accounts/notifications/sms/phone_verification_message.body.txt",
            context_name="phone_verification_context",
            context_kwargs=NotificationContextDict(
                {"user_id": user.id, "phone_verification_code": code, "phone_number": phone}
            ),
        )

    def send_unknown_account_sms(self, phone: str | None, **kwargs) -> None:
        """
        In case enumeration prevention is enabled, and, a verification code is requested for an
        unlisted phone number, this method is invoked to send a text explaining that no account
        is on file.
        """
        if not phone:
            logger.warning("No phone number provided for sending unknown account SMS.")
            return

        self.notification_service.create_one_off_notification(
            email_or_phone=phone,
            notification_type=NotificationTypes.SMS.value,
            title="Phone Verification Unknown Account Message",
            body_template="accounts/notifications/sms/unknown_account.body.txt",
            context_name="phone_verification_error_context",
            context_kwargs=NotificationContextDict({"phone_number": phone}),
        )

    def send_account_already_exists_sms(self, phone: str | None, **kwargs) -> None:
        """
        In case enumeration prevention is enabled, and, a signup is attempted using a phone
        number that already has an account, this method is invoked to send a text explaining
        that an account is already on file (instead of revealing this via the signup response).
        """
        if not phone:
            logger.warning("No phone number provided for sending account-already-exists SMS.")
            return

        self.notification_service.create_one_off_notification(
            email_or_phone=phone,
            notification_type=NotificationTypes.SMS.value,
            title="Phone Verification Account already exists Message",
            body_template="accounts/notifications/sms/account_already_exists.body.txt",
            context_name="phone_verification_error_context",
            context_kwargs=NotificationContextDict({"phone_number": phone}),
        )


class HeadlessAdapter(DefaultHeadlessAdapter):
    """
    Custom adapter for headless authentication.
    """

    def serialize_user(self, user):
        """
        Serialize the user object to a dictionary.
        """
        from allauth.socialaccount.adapter import get_adapter

        data = get_adapter().serialize_instance(user)
        data["has_usable_password"] = user.has_usable_password()
        return data

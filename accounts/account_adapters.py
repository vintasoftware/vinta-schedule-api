import datetime
import logging
from copy import deepcopy
from typing import Annotated

from django.conf import settings
from django.urls import reverse

from allauth.account.adapter import DefaultAccountAdapter
from allauth.headless.adapter import DefaultHeadlessAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.utils import build_absolute_uri
from dependency_injector.wiring import Provide, inject
from twilio.rest import Client
from vintasend.constants import NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService

from users.models import Profile, User


logger = logging.getLogger(__name__)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_connect_redirect_url(self, request, socialaccount):
        return reverse("index")

    def serialize_instance(self, instance):
        if isinstance(instance, SocialLogin):
            serialized_social_login = super().serialize_instance(instance)
            serialized_social_login["user"] = self.serialize_instance(instance.user)
            return serialized_social_login

        if isinstance(instance, User):
            # If the instance is a User, use the UserSerializer to serialize it
            return {
                "id": instance.id,
                "username": instance.username,
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
                    "first_name": instance.profile.first_name,
                    "last_name": instance.profile.last_name,
                    "profile_picture": instance.profile.profile_picture.url
                    if instance.profile.profile_picture
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
        **kwargs,
    ):
        self.notification_service = notification_service
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
            user.username,
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

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        client.messages.create(
            body="Your phone number is not associated with any account.",
            from_=settings.TWILIO_NUMBER,
            to=phone,
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

        return get_adapter().serialize_instance(user)

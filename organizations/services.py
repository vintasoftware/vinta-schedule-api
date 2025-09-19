import datetime
from typing import Annotated

from django.db import transaction
from django.urls import reverse

from allauth.utils import build_absolute_uri
from dependency_injector.wiring import Provide, inject
from psycopg import IntegrityError
from vintasend.services.notification_service import (
    NotificationContextDict,
    NotificationService,
    NotificationTypes,
)

from calendar_integration.services.calendar_service import CalendarService
from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from users.models import User


class OrganizationService:
    @inject
    def __init__(
        self,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
        notification_service: Annotated[NotificationService, Provide["notification_service"]],
    ):
        self.calendar_service = calendar_service
        self.notification_service = notification_service

    def create_organization(
        self, creator: User, name: str, should_sync_rooms: bool = False
    ) -> Organization:
        """
        Create a new calendar organization.
        :param name: Name of the calendar organization.
        :param should_sync_rooms: Whether to sync rooms for this organization.
        :return: Created Organization instance.
        """
        self.organization = Organization.objects.create(
            name=name,
            should_sync_rooms=should_sync_rooms,
        )
        OrganizationMembership.objects.create(user=creator, organization=self.organization)

        if should_sync_rooms:
            self.calendar_service.initialize_without_provider(
                user_or_token=creator, organization=self.organization
            )
            now = datetime.datetime.now(tz=datetime.UTC)
            self.calendar_service.request_organization_calendar_resources_import(
                start_time=now,
                end_time=now + datetime.timedelta(days=365),
            )
        return self.organization

    @transaction.atomic()
    def invite_user_to_organization(
        self,
        email: str,
        first_name: str,
        last_name: str,
        invited_by: User,
        organization: Organization,
    ) -> None:
        """
        Invite a user to join the organization. if the invitation already exists, resets the token
        and the expiration date.
        :param email: Email of the user to invite.
        :param invited_by: User who is sending the invitation.
        """
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        now = datetime.datetime.now(tz=datetime.UTC)
        seven_days_from_now = now + datetime.timedelta(days=7)
        try:
            invitation, created = OrganizationInvitation.objects.get_or_create(
                email=email,
                organization=organization,
                accepted_at__isnull=True,
                membership__isnull=True,
                defaults={
                    "invited_by": invited_by,
                    "first_name": first_name,
                    "last_name": last_name,
                    "token_hash": token_hash,
                    "expires_at": seven_days_from_now,
                },
            )
        except IntegrityError as e:
            # Handle the case where the invitation with this email already exists
            raise ValueError("An active invitation for this email already exists.") from e

        if not created:
            invitation.token_hash = token_hash
            invitation.expires_at = seven_days_from_now
            invitation.invited_by = invited_by
            invitation.first_name = first_name
            invitation.last_name = last_name
            invitation.accepted_at = None
            invitation.membership = None
            invitation.save()

        transaction.on_commit(
            lambda: self.notification_service.create_one_off_notification(
                email_or_phone=email,
                first_name=first_name,
                last_name=last_name,
                notification_type=NotificationTypes.EMAIL.value,
                title="Invitation to join organization",
                body_template="organizations/emails/organization_invitation.body.html",
                context_name="organization_invitation_context",
                context_kwargs=NotificationContextDict(
                    {
                        "organization_invitation_id": invitation.id,
                        "invitation_url": (
                            build_absolute_uri(reverse("invitation", args=[token])),
                        ),
                    }
                ),
                subject_template="organizations/emails/organization_invitation.subject.txt",
            )
        )

    def accept_invitation(self, token: str, user: User) -> OrganizationMembership:
        """
        Accept an invitation to join an organization.
        :param token: Invitation token.
        :param user: User who is accepting the invitation.
        :return: Created OrganizationMembership instance.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        invitations = OrganizationInvitation.objects.filter(email=user.email, expires_at__gt=now)
        for invitation in invitations:
            if verify_long_lived_token(token, invitation.token_hash):
                membership = OrganizationMembership.objects.create(
                    user=user, organization=invitation.organization
                )
                invitation.accepted_at = now
                invitation.membership = membership
                invitation.save()
                return membership

        raise ValueError("Invalid or expired token")

    def revoke_invitation(self, invitation_id: str) -> None:
        """
        Revoke an invitation to join an organization.
        :param invitation_id: ID of the invitation to revoke.
        """
        try:
            invitation = OrganizationInvitation.objects.get(id=invitation_id)
            invitation.expires_at = datetime.datetime.now(tz=datetime.UTC)
            invitation.save()
        except OrganizationInvitation.DoesNotExist as e:
            raise ValueError("Invitation does not exist") from e

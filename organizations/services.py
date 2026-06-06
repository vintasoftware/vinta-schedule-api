import datetime
import logging
from typing import Annotated

from django.db import IntegrityError, transaction
from django.urls import reverse

from allauth.socialaccount.models import SocialAccount
from allauth.utils import build_absolute_uri
from dependency_injector.wiring import Provide, inject
from vintasend.services.notification_service import (
    NotificationContextDict,
    NotificationService,
    NotificationTypes,
)

from calendar_integration.models import (
    Calendar,
    CalendarOwnership,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_service import CalendarService
from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from organizations.exceptions import (
    DuplicateInvitationError,
    InvalidInvitationTokenError,
    InvitationNotFoundError,
    NoServiceAccountConfiguredError,
    UserAlreadyHasMembershipError,
)
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from users.models import User


logger = logging.getLogger(__name__)


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
        # The creator of the organization is its first admin — every org must
        # have at least one admin, and no one else exists yet to promote them.
        OrganizationMembership.objects.create(
            user=creator,
            organization=self.organization,
            role=OrganizationRole.ADMIN,
        )

        if should_sync_rooms:
            # A newly created organization cannot have a service account yet, so
            # request_rooms_sync will raise NoServiceAccountConfiguredError.  We
            # catch it here and log a warning instead of crashing — org creation
            # must always succeed; the admin can trigger the sync later once they
            # have configured a Google service account via PATCH.
            try:
                self.request_rooms_sync(
                    organization=self.organization,
                    requested_by=creator,
                )
            except NoServiceAccountConfiguredError:
                logger.warning(
                    "Skipping rooms sync for new organization %s: no service account configured.",
                    self.organization.id,
                )
        return self.organization

    def request_rooms_sync(
        self,
        organization: Organization,
        requested_by: User,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
    ) -> None:
        """Authenticate with the org's Google service account and enqueue a
        calendar resources import for the given organization.

        Resolves the org-level ``GoogleCalendarServiceAccount`` (the one without
        a ``calendar`` FK).  If none is configured, raises
        ``NoServiceAccountConfiguredError`` (a DRF ValidationError / 400) so
        callers can surface a clean error rather than a 500.

        :param organization: The organization to sync rooms for.
        :param requested_by: The user (or token) authorizing the sync.
        :param start_time: Import window start; defaults to now.
        :param end_time: Import window end; defaults to now + 365 days.
        :raises NoServiceAccountConfiguredError: When no service account is
            configured for the organization.
        """
        service_account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(organization.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        if service_account is None:
            raise NoServiceAccountConfiguredError()

        self.calendar_service.authenticate(account=service_account, organization=organization)
        now = datetime.datetime.now(tz=datetime.UTC)
        self.calendar_service.request_organization_calendar_resources_import(
            start_time=start_time or now,
            end_time=end_time or (now + datetime.timedelta(days=365)),
        )

    def request_all_calendars_sync(
        self,
        organization: Organization,
        requested_by: User,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
    ) -> dict[str, list]:
        """Enqueue a sync for every active calendar in the organization.

        Each active calendar is synced using its owner's linked provider account
        (mirroring admin-sync): the default ``CalendarOwnership`` resolves the
        owner, and the owner's ``SocialAccount`` for the calendar's provider
        authenticates the sync. Calendars with no owner or no matching linked
        account are reported under ``skipped`` rather than failing the request.

        :param organization: The organization whose calendars should be synced.
        :param requested_by: The admin authorizing the sync.
        :param start_datetime: Sync window start.
        :param end_datetime: Sync window end.
        :param should_update_events: Whether to update existing events.
        :return: ``{"synced": [calendar_id, ...], "skipped": [{"calendar_id", "reason"}, ...]}``.
        """
        synced: list[int] = []
        skipped: list[dict] = []

        calendars = Calendar.objects.filter_by_organization(organization.id).filter(is_active=True)

        for calendar in calendars:
            ownership = (
                CalendarOwnership.objects.filter_by_organization(organization.id)
                .filter(calendar=calendar)
                .order_by("-is_default", "id")
                .first()
            )
            if ownership is None:
                skipped.append({"calendar_id": calendar.id, "reason": "no owner"})
                continue

            social_account = SocialAccount.objects.filter(
                user=ownership.user, provider=calendar.provider
            ).first()
            if social_account is None:
                skipped.append(
                    {
                        "calendar_id": calendar.id,
                        "reason": f"owner has no linked {calendar.provider} account",
                    }
                )
                continue

            self.calendar_service.authenticate(account=social_account, organization=organization)
            self.calendar_service.request_calendar_sync(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                should_update_events=should_update_events,
            )
            synced.append(calendar.id)

        return {"synced": synced, "skipped": skipped}

    @transaction.atomic()
    def invite_user_to_organization(
        self,
        email: str,
        first_name: str,
        last_name: str,
        invited_by: User,
        organization: Organization,
    ) -> OrganizationInvitation:
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
            raise DuplicateInvitationError() from e

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
        return invitation

    def accept_invitation(self, token: str, user: User) -> OrganizationMembership:
        """
        Accept an invitation to join an organization.

        Raises UserAlreadyHasMembershipError if the user is already a member of any
        organization before the membership create is attempted, preventing an IntegrityError
        from the OneToOne constraint on OrganizationMembership.user.

        :param token: Invitation token.
        :param user: User who is accepting the invitation.
        :return: Created OrganizationMembership instance.
        """
        # DESIGN: hasattr check intentionally matches BOTH active and inactive memberships.
        # An inactive membership still has a DB row, so accept_invitation must refuse
        # re-provisioning here.  The supported un-disable path is admin REACTIVATION
        # (setting is_active=True); not re-accepting an invitation.
        if hasattr(user, "organization_membership"):
            raise UserAlreadyHasMembershipError()

        now = datetime.datetime.now(tz=datetime.UTC)
        invitations = OrganizationInvitation.objects.filter(
            email__iexact=user.email, expires_at__gt=now
        )
        for invitation in invitations:
            if verify_long_lived_token(token, invitation.token_hash):
                try:
                    with transaction.atomic():
                        membership = OrganizationMembership.objects.create(
                            user=user, organization=invitation.organization
                        )
                except IntegrityError as e:
                    raise UserAlreadyHasMembershipError() from e
                invitation.accepted_at = now
                invitation.membership = membership
                invitation.save()
                return membership

        raise InvalidInvitationTokenError()

    @transaction.atomic()
    def provision_tenant_for_user(
        self, user: User, organization_name: str | None = None
    ) -> OrganizationMembership | None:
        """
        Provision a tenant for a user who does not yet have a membership.

        This is the single guarded entry point for turning a membership-less user into a
        member. It is called by every signup path (email confirmation, social adapter,
        explicit invite accept) so the provisioning decision is centralised and can never
        silently create a second membership.

        Logic (in order):
        1. If the user already has a membership → raise UserAlreadyHasMembershipError.
        2. Else if a non-expired, unaccepted OrganizationInvitation exists for user.email
           → create a MEMBER membership in the inviting org and mark the invitation
           accepted.
        3. Else if organization_name is truthy → delegate to create_organization (user
           becomes ADMIN of a new org).
        4. Else → return None (caller decides — gated onboarding).

        :param user: The user to provision a tenant for.
        :param organization_name: Optional name of the new organization to create when no
            pending invitation is found.
        :return: The created OrganizationMembership on the join/create branches; None on
            the no-op branch.
        :raises UserAlreadyHasMembershipError: When the user already belongs to an
            organization.
        """
        # DESIGN: hasattr check intentionally matches BOTH active and inactive memberships.
        # An inactive membership still has a DB row, so provision_tenant_for_user must
        # refuse re-provisioning here.  The supported un-disable path is admin REACTIVATION
        # (setting is_active=True); not re-provisioning the user.
        if hasattr(user, "organization_membership"):
            raise UserAlreadyHasMembershipError()

        now = datetime.datetime.now(tz=datetime.UTC)
        pending_invitation = OrganizationInvitation.objects.filter(
            email__iexact=user.email,
            expires_at__gt=now,
            accepted_at__isnull=True,
            membership__isnull=True,
        ).first()

        if pending_invitation is not None:
            try:
                with transaction.atomic():
                    membership = OrganizationMembership.objects.create(
                        user=user,
                        organization=pending_invitation.organization,
                        role=OrganizationRole.MEMBER,
                    )
            except IntegrityError as e:
                raise UserAlreadyHasMembershipError() from e
            pending_invitation.accepted_at = now
            pending_invitation.membership = membership
            pending_invitation.save()
            return membership

        if organization_name:
            try:
                with transaction.atomic():
                    organization = self.create_organization(creator=user, name=organization_name)
            except IntegrityError as e:
                raise UserAlreadyHasMembershipError() from e
            return organization.memberships.get(user=user)

        return None

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
            raise InvitationNotFoundError() from e

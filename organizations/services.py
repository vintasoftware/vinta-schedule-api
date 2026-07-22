import datetime
import logging
from typing import Annotated

from django.conf import settings
from django.db import IntegrityError, transaction

from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from vintasend.services.notification_service import (
    NotificationContextDict,
    NotificationService,
    NotificationTypes,
)

from audit.constants import AuditAction
from audit.diff import compute_diff
from audit.services import AuditService
from calendar_integration.constants import CalendarSyncTriggerSource
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
from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService
from users.models import User
from webhooks.services.webhook_membership_side_effects import WebhookMembershipSideEffectsService


logger = logging.getLogger(__name__)


class OrganizationService:
    @inject
    def __init__(
        self,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
        notification_service: Annotated[NotificationService, Provide["notification_service"]],
        webhook_membership_side_effects_service: Annotated[
            WebhookMembershipSideEffectsService,
            Provide["webhook_membership_side_effects_service"],
        ],
        audit_service: Annotated[AuditService, Provide["audit_service"]],
        subscription_service: Annotated[SubscriptionService, Provide["subscription_service"]],
        entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
    ):
        self.calendar_service = calendar_service
        self.notification_service = notification_service
        self.webhook_membership_side_effects_service = webhook_membership_side_effects_service
        self.audit_service = audit_service
        self.subscription_service = subscription_service
        self.entitlement_service = entitlement_service

    @transaction.atomic()
    def create_organization(
        self,
        creator: User,
        name: str,
        should_sync_rooms: bool = False,
        external_event_update_policy: str | None = None,
    ) -> Organization:
        """
        Create a new calendar organization.
        :param name: Name of the calendar organization.
        :param should_sync_rooms: Whether to sync rooms for this organization.
        :param external_event_update_policy: Policy for inbound external provider
            edits/deletions. When ``None`` the model's default is used.
        :return: Created Organization instance.

        Wrapped in its own transaction (rather than relying on the caller's) so the
        "no plan-less state" invariant does not depend on the call site: under
        ``ATOMIC_REQUESTS`` the DRF/view caller already wraps this, but a
        management command, shell, or Celery task calling this directly would
        otherwise be able to commit the ``Organization`` row and then fail on
        subscription creation, leaving a plan-less organization behind.
        """
        create_kwargs: dict = {
            "name": name,
            "should_sync_rooms": should_sync_rooms,
        }
        if external_event_update_policy is not None:
            create_kwargs["external_event_update_policy"] = external_event_update_policy
        self.organization = Organization.objects.create(**create_kwargs)
        # Every organization always has exactly one active plan, from creation —
        # there is no plan-less state.
        # A no-op for a reseller child (parent set): it pools against its root's
        # subscription instead. See SubscriptionService.create_subscription_for_organization.
        self.subscription_service.create_subscription_for_organization(self.organization)
        # The creator of the organization is its first admin — every org must
        # have at least one admin, and no one else exists yet to promote them.
        admin_membership = OrganizationMembership.objects.create(
            user=creator,
            organization=self.organization,
            role=OrganizationRole.ADMIN,
        )
        self.webhook_membership_side_effects_service.on_member_created(admin_membership)

        # Audit: the creator (now the org's first admin) is the actor for both the
        # organization creation and their own admin membership.
        actor = self.audit_service.actor_from_membership(admin_membership)
        self.audit_service.record(
            organization_id=self.organization.id,
            action=AuditAction.CREATE,
            actor=actor,
            subject=self.audit_service.subject_from_instance(self.organization),
        )
        self.audit_service.record(
            organization_id=self.organization.id,
            action=AuditAction.CREATE,
            actor=actor,
            subject=self.audit_service.subject_from_instance(admin_membership),
            affected_membership_ids=[admin_membership.user_id],
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

        calendars = Calendar.objects.filter_by_organization(organization.id).exclude_inactive()

        for calendar in calendars:
            ownership = (
                CalendarOwnership.objects.filter_by_organization(organization.id)
                .filter(calendar=calendar, membership_user_id__isnull=False)
                .order_by("-is_default", "id")
                .first()
            )
            if ownership is None:
                skipped.append({"calendar_id": calendar.id, "reason": "no owner"})
                continue

            social_account = SocialAccount.objects.filter(
                user_id=ownership.membership_user_id, provider=calendar.provider
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
            sync = self.calendar_service.request_calendar_sync(
                calendar=calendar,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                should_update_events=should_update_events,
                trigger_source=CalendarSyncTriggerSource.ADMIN,
            )
            if sync is None:
                # Calendar has sync disabled (e.g. an imported read-only calendar).
                skipped.append({"calendar_id": calendar.id, "reason": "sync disabled"})
                continue
            synced.append(calendar.id)

        return {"synced": synced, "skipped": skipped}

    @transaction.atomic()
    def invite_user_to_organization(
        self,
        email: str,
        first_name: str,
        last_name: str,
        organization: Organization,
        invited_by: User | None = None,
        role: str = OrganizationRole.MEMBER,
        send_email: bool = True,
        bypass_limits: bool = False,
    ) -> OrganizationInvitation:
        """
        Invite a user to join the organization. if the invitation already exists, resets the token
        and the expiration date.

        The raw token is attached to the returned invitation as ``invitation._raw_token`` (a
        transient, non-persisted attribute) so callers that need it (e.g. the public-API
        self-managed-invitation path) can surface it once without it ever being stored in
        plaintext. Only ``token_hash`` is persisted.

        :param email: Email of the user to invite.
        :param organization: Organization the user is being invited to.
        :param invited_by: User who is sending the invitation. May be None when the invitation is
            created by an API-level actor (e.g. a reseller via the public GraphQL API) that has no
            corresponding Django User.
        :param role: Role the invited user should receive on accepting the invitation. Defaults to
            MEMBER. Use OrganizationRole.ADMIN for admin invitations.
        :param send_email: When True (default) the invitation email is dispatched via the
            notification service. When False the email is suppressed — the caller is responsible
            for delivering the invite link using the raw token attached to the returned instance
            as ``_raw_token``.
        :param bypass_limits: When True, skips the seat-limit guard below. Only management
            commands and one-off repair scripts should pass this — never a request-handling path.
        :raises OverLimitError: When the organization is at its effective seat limit
            (``organization_members``: active memberships plus other pending invitations).
            Nothing is created. Checked and locked (``SELECT ... FOR UPDATE`` on the billing
            root's subscription) inside this method's own transaction, so two concurrent
            invitations for the last seat serialize on that row and exactly one succeeds.

        A **resend** (re-inviting the same still-pending email/organization pair, which resets
        the token/expiry rather than creating a new row) resolves the still-pending invitation
        being reused *before* the guard and excludes it from the count
        (``EntitlementService.check_limit(..., exclude_invitation_id=...)``) — a resend is
        net-zero on seats, exactly like an accept, since it creates nothing new. A genuinely
        new invitation (no matching pending row) is still checked and blocked at the ceiling
        as before.
        """
        # Guard first, before anything is written in this transaction — an
        # OverLimitError raised after a write would rely on the caller's exception
        # handler to roll the request transaction back (REST does; a caller that
        # does not would silently commit a rejected invitation).
        if not bypass_limits:

            def _resolve_existing_invitation_id() -> int | None:
                # A resend reuses the still-pending row `get_or_create` below would
                # find (same email/organization, not yet accepted); excluding it from
                # the count makes the resend net-zero rather than a false block at the
                # exact ceiling. Resolved lazily -- only called once the ceiling is
                # known to be finite -- so an `unlimited` organization never pays for
                # this extra query.
                existing_invitation = OrganizationInvitation.objects.filter(
                    organization=organization,
                    email=email,
                    accepted_at__isnull=True,
                    membership_user_id__isnull=True,
                ).first()
                return existing_invitation.pk if existing_invitation is not None else None

            result = self.entitlement_service.check_limit(
                organization,
                LimitedResource.ORGANIZATION_MEMBERS,
                lock=True,
                exclude_invitation_id_resolver=_resolve_existing_invitation_id,
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        now = datetime.datetime.now(tz=datetime.UTC)
        seven_days_from_now = now + datetime.timedelta(days=7)
        try:
            invitation, created = OrganizationInvitation.objects.get_or_create(
                email=email,
                organization=organization,
                accepted_at__isnull=True,
                membership_user_id__isnull=True,
                defaults={
                    "invited_by": invited_by,
                    "first_name": first_name,
                    "last_name": last_name,
                    "token_hash": token_hash,
                    "expires_at": seven_days_from_now,
                    "role": role,
                },
            )
        except IntegrityError as e:
            # Handle the case where the invitation with this email already exists
            raise DuplicateInvitationError() from e

        diff = None
        if not created:
            before = {
                "expires_at": invitation.expires_at.isoformat() if invitation.expires_at else None,
                "role": invitation.role,
                "accepted_at": invitation.accepted_at.isoformat()
                if invitation.accepted_at
                else None,
            }
            invitation.token_hash = token_hash
            invitation.expires_at = seven_days_from_now
            invitation.invited_by = invited_by
            invitation.first_name = first_name
            invitation.last_name = last_name
            invitation.role = role
            invitation.accepted_at = None
            invitation.membership_user_id = None
            invitation.save()
            after = {
                "expires_at": seven_days_from_now.isoformat(),
                "role": role,
                "accepted_at": None,
            }
            diff = compute_diff(before, after)

        # Audit: an admin (or an API-level actor with no Django User) invites a user.
        # A fresh invitation is a CREATE; reusing an existing pending row is an UPDATE
        # (token/expiry/role reset).
        actor = (
            self.audit_service.actor_from_user(invited_by, organization.id)
            if invited_by is not None
            else self.audit_service.system_actor()
        )
        self.audit_service.record(
            organization_id=organization.id,
            action=AuditAction.CREATE if created else AuditAction.UPDATE,
            actor=actor,
            subject=self.audit_service.subject_from_instance(invitation),
            diff=diff,
        )

        # Attach the raw token as a transient attribute so the caller can surface it once.
        # It is never persisted — only token_hash is stored in the DB row.
        invitation._raw_token = token  # type: ignore[attr-defined]

        if send_email:
            # TODO: set From=support_email when branded.
            # resolve_branding(invitation.organization) gives the reseller's support_email,
            # but DjangoEmailNotificationAdapter.send() always uses
            # NOTIFICATION_DEFAULT_FROM_EMAIL and does not accept a per-notification
            # from_email override. Connecting this requires extending the vintasend
            # adapter API.
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
                            "invitation_url": (getattr(settings, "HEADLESS_FRONTEND_URLS", {}))
                            .get("account_accept_invitation", "")
                            .format(token=token),
                        }
                    ),
                    subject_template="organizations/emails/organization_invitation.subject.txt",
                    preheader_template="organizations/emails/organization_invitation.pre_header.txt",
                )
            )
        return invitation

    def accept_invitation(
        self, token: str, user: User, bypass_limits: bool = False
    ) -> OrganizationMembership:
        """
        Accept an invitation to join an organization.

        Raises UserAlreadyHasMembershipError only when the user is already a
        member of the *specific* organization named in the matched invitation. A
        user in org A can accept a valid invitation from org B and gain a second
        membership. The composite unique constraint ``uniq_membership_user_organization``
        (user, organization) on OrganizationMembership is the DB safety net for
        any race that slips past the per-org pre-check.

        :param token: Invitation token.
        :param user: User who is accepting the invitation.
        :param bypass_limits: When True, skips the seat-limit guard below. Only management
            commands and one-off repair scripts should pass this — never a request-handling path.
        :return: Created OrganizationMembership instance.
        :raises UserAlreadyHasMembershipError: When the user is already a member of the
            invitation's organization (same-org duplicate).
        :raises InvalidInvitationTokenError: When no valid, non-expired invitation
            matches the token and the user's email.
        :raises OverLimitError: When accepting would take the organization over its seat
            limit. Uses ``EntitlementService.check_seat_limit_for_invitation_accept`` rather
            than the generic ``check_limit`` — accepting is net-zero on seats (the pending
            invitation stops being pending and becomes the membership it already reserved
            capacity for), so counting it on both sides would make an organization unable to
            ever accept its own last outstanding invitation.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        invitations = OrganizationInvitation.objects.filter(
            email__iexact=user.email, expires_at__gt=now
        )
        for invitation in invitations:
            if verify_long_lived_token(token, invitation.token_hash):
                # Per-org check: refuse only a duplicate in the SAME org.
                # A user already in a different org is allowed to join this one.
                if user.organization_memberships.filter(
                    organization=invitation.organization
                ).exists():
                    raise UserAlreadyHasMembershipError()
                with transaction.atomic():
                    # Guard first, before the membership write, inside the same
                    # transaction as the create — see check_limit's lock docs.
                    if not bypass_limits:
                        check_seat_limit = (
                            self.entitlement_service.check_seat_limit_for_invitation_accept
                        )
                        result = check_seat_limit(invitation)
                        if not result.allowed:
                            raise OverLimitError.from_check_result(result)
                    # Narrowed to just the create: an IntegrityError from the
                    # invitation.save() below is a different failure (e.g. a
                    # constraint on the invitation row itself) and must not be
                    # reported as "you already have a membership" — that would be
                    # a wrong, confusing error for something unrelated.
                    try:
                        membership = OrganizationMembership.objects.create(
                            user=user,
                            organization=invitation.organization,
                            role=invitation.role,
                        )
                    except IntegrityError as e:
                        raise UserAlreadyHasMembershipError() from e
                    # Marking the invitation accepted must land in the same
                    # transaction as the guard + membership create, not a
                    # separate autocommit write after the block exits. Outside
                    # a request (Celery, management command, shell) there is no
                    # outer ATOMIC_REQUESTS transaction to fold this into: the
                    # membership create would commit and release the row lock,
                    # then a concurrent check could see the seat still "pending"
                    # and count it twice, before this line ever runs — and if
                    # this write then failed, the double-count would be
                    # permanent.
                    invitation.accepted_at = now
                    invitation.membership_user_id = membership.user_id
                    invitation.save()
                self.webhook_membership_side_effects_service.on_member_created(membership)

                # Audit: the accepting user joins the org (new membership) and the
                # invitation transitions to accepted. The user is the actor for both.
                actor = self.audit_service.actor_from_membership(membership)
                self.audit_service.record(
                    organization_id=invitation.organization_id,
                    action=AuditAction.CREATE,
                    actor=actor,
                    subject=self.audit_service.subject_from_instance(membership),
                    affected_membership_ids=[membership.user_id],
                )
                self.audit_service.record(
                    organization_id=invitation.organization_id,
                    action=AuditAction.UPDATE,
                    actor=actor,
                    subject=self.audit_service.subject_from_instance(invitation),
                    diff={"accepted_at": {"old": None, "new": now.isoformat()}},
                )
                return membership

        raise InvalidInvitationTokenError()

    @transaction.atomic()
    def provision_tenant_for_user(
        self,
        user: User,
        organization_name: str | None = None,
        bypass_limits: bool = False,
    ) -> OrganizationMembership | None:
        """
        Provision a tenant for a user on the signup / invite-accept path.

        The pending-invitation branch has no blanket "user has any membership → refuse"
        check, so a user already in org A can accept a pending invitation to org B via
        the signup adapter. The ``organization_name`` branch (create-new-org on signup)
        keeps its check: auto-creating an additional org for an existing member is
        handled by ``OrganizationManagementPermission`` on the create endpoint, not this
        path.

        Logic (in order):
        1. If a non-expired, unaccepted OrganizationInvitation exists for user.email:
           a. If the user is already a member of the inviting org → raise
              UserAlreadyHasMembershipError (same-org duplicate).
           b. Otherwise → create a MEMBER membership in the inviting org and mark the
              invitation accepted (allows joining a new org even when already a member
              elsewhere).
        2. Else if organization_name is truthy:
           a. If the user already has ANY membership → raise UserAlreadyHasMembershipError.
              Creating an additional org on the signup path is out of scope here.
           b. Otherwise → delegate to create_organization (user becomes ADMIN of a new org).
        3. Else → return None (caller decides — gated onboarding).

        :param user: The user to provision a tenant for.
        :param organization_name: Optional name of the new organization to create when no
            pending invitation is found.
        :param bypass_limits: When True, skips the seat-limit guard on the pending-invitation
            (join) branch below. Only management commands and one-off repair scripts should
            pass this — never a request-handling path. The ``organization_name`` (create) branch
            is never seat-limited: it makes a brand-new organization with a single admin
            membership, not a join against an existing organization's ceiling.
        :return: The created OrganizationMembership on the join/create branches; None on
            the no-op branch.
        :raises UserAlreadyHasMembershipError: On the pending-invitation branch, when the
            user is already a member of the invitation's organization.  On the
            organization_name branch, when the user already belongs to any organization.
        :raises OverLimitError: On the pending-invitation branch, when joining would take the
            organization over its seat limit. Uses the same net-zero-safe
            ``check_seat_limit_for_invitation_accept`` as ``accept_invitation`` — this branch is
            the signup-path equivalent of accepting an invitation.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        pending_invitation = OrganizationInvitation.objects.filter(
            email__iexact=user.email,
            expires_at__gt=now,
            accepted_at__isnull=True,
            membership_user_id__isnull=True,
        ).first()

        if pending_invitation is not None:
            # Per-org check: refuse only a duplicate in the SAME org.
            # A user already in a different org is allowed to join the inviting org.
            if user.organization_memberships.filter(
                organization=pending_invitation.organization
            ).exists():
                raise UserAlreadyHasMembershipError()
            with transaction.atomic():
                # Guard first, before the membership write, inside the same
                # transaction as the create — see check_limit's lock docs.
                if not bypass_limits:
                    result = self.entitlement_service.check_seat_limit_for_invitation_accept(
                        pending_invitation
                    )
                    if not result.allowed:
                        raise OverLimitError.from_check_result(result)
                # Narrowed to just the create: an IntegrityError from the
                # pending_invitation.save() below is a different failure and must
                # not be reported as "you already have a membership".
                try:
                    membership = OrganizationMembership.objects.create(
                        user=user,
                        organization=pending_invitation.organization,
                        role=pending_invitation.role,
                    )
                except IntegrityError as e:
                    raise UserAlreadyHasMembershipError() from e
                # Marking the invitation accepted must land in the same
                # transaction as the guard + membership create, not a separate
                # autocommit write after the block exits. Outside a request
                # (Celery, management command, shell) there is no outer
                # ATOMIC_REQUESTS transaction to fold this into: the membership
                # create would commit and release the row lock, then a concurrent
                # check could see the seat still "pending" and count it twice,
                # before this line ever runs — and if this write then failed, the
                # double-count would be permanent.
                pending_invitation.accepted_at = now
                pending_invitation.membership_user_id = membership.user_id
                pending_invitation.save()
            self.webhook_membership_side_effects_service.on_member_created(membership)

            # Audit: user joins the inviting org via the signup path; same shape as
            # accept_invitation (membership CREATE + invitation accepted UPDATE).
            actor = self.audit_service.actor_from_membership(membership)
            self.audit_service.record(
                organization_id=pending_invitation.organization_id,
                action=AuditAction.CREATE,
                actor=actor,
                subject=self.audit_service.subject_from_instance(membership),
                affected_membership_ids=[membership.user_id],
            )
            self.audit_service.record(
                organization_id=pending_invitation.organization_id,
                action=AuditAction.UPDATE,
                actor=actor,
                subject=self.audit_service.subject_from_instance(pending_invitation),
                diff={"accepted_at": {"old": None, "new": now.isoformat()}},
            )
            return membership

        if organization_name:
            # Auto-creating a second org for an existing member is handled by the create
            # endpoint (OrganizationManagementPermission on POST /organizations/).
            # This signup-path branch keeps the original single-membership check.
            if user.organization_memberships.exists():
                raise UserAlreadyHasMembershipError()
            try:
                with transaction.atomic():
                    organization = self.create_organization(creator=user, name=organization_name)
            except IntegrityError as e:
                raise UserAlreadyHasMembershipError() from e
            return organization.memberships.get(user=user)

        return None

    def revoke_invitation(self, invitation_id: str, bypass_limits: bool = False) -> None:
        """
        Revoke an invitation to join an organization.
        :param invitation_id: ID of the invitation to revoke.
        :param bypass_limits: When True, skips the restricted-organization check
            below. Only management commands and one-off repair scripts should
            pass this — never a request-handling path.
        :raises OverLimitError: When the invitation's organization is restricted
            — a restricted org may not write, including revoking one
            of its own invitations. Read-then-check, not check-then-read: the
            organization to check is only known once the invitation is resolved.
        """
        try:
            invitation = OrganizationInvitation.objects.get(id=invitation_id)
            if not bypass_limits:
                self.entitlement_service.check_not_restricted(invitation.organization)
            old_expires_at = invitation.expires_at
            now = datetime.datetime.now(tz=datetime.UTC)
            invitation.expires_at = now
            invitation.save()
        except OrganizationInvitation.DoesNotExist as e:
            raise InvitationNotFoundError() from e

        # Audit: revoking an invitation expires it immediately. No acting User is
        # threaded into this method, so the actor is the system.
        self.audit_service.record(
            organization_id=invitation.organization_id,
            action=AuditAction.UPDATE,
            actor=self.audit_service.system_actor(),
            subject=self.audit_service.subject_from_instance(invitation),
            diff={
                "expires_at": {
                    "old": old_expires_at.isoformat() if old_expires_at else None,
                    "new": now.isoformat(),
                }
            },
        )

    @transaction.atomic()
    def reactivate_membership(
        self,
        membership: OrganizationMembership,
        bypass_limits: bool = False,
    ) -> OrganizationMembership:
        """Reactivate a member (set is_active=True). The seat-limit check lives here in
        the service layer rather than at the viewset, matching the ``bypass_limits``
        convention every limit-checked method in this module follows.

        Reactivating occupies a seat again (``OrganizationMembershipQuerySet
        .occupying_a_seat`` only counts ``is_active=True`` rows), so — unlike every
        other membership-lifecycle write — it is a capacity-raising write with no
        accompanying ``OrganizationInvitation``/membership *create*, and would
        otherwise be an unmetered path onto the ``organization_members`` limit.
        Only checked when the member is currently inactive: an already-active
        member is a no-op and must stay one even if the organization is at its
        limit for an unrelated reason.

        :param membership: The membership to reactivate.
        :param bypass_limits: When True, skips the seat-limit guard below. Only
            management commands and one-off repair scripts should pass this —
            never a request-handling path.
        :return: The (possibly already-active) membership, reactivated.
        :raises OverLimitError: When reactivating would take the organization over
            its seat limit.

        Idempotency: reactivating an already-active member is a no-op success.
        """
        if not membership.is_active:
            if not bypass_limits:
                result = self.entitlement_service.check_limit(
                    membership.organization, LimitedResource.ORGANIZATION_MEMBERS, lock=True
                )
                if not result.allowed:
                    raise OverLimitError.from_check_result(result)
            membership.is_active = True
            membership.save(update_fields=["is_active"])
        return membership

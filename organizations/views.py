import datetime
import logging
from typing import Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from calendar_integration.models import GoogleCalendarServiceAccount
from calendar_integration.serializers import CalendarSyncRequestSerializer
from common.utils.view_utils import (
    NoListVintaScheduleModelViewSet,
    NoUpdateVintaScheduleModelViewSet,
    ReadOnlyVintaScheduleModelViewSet,
)
from organizations.exceptions import (
    DuplicateInvitationError,
    InvalidInvitationTokenError,
    InvitationNotFoundError,
    NoServiceAccountConfiguredError,
    UserAlreadyHasMembershipError,
)
from organizations.filtersets import (
    OrganizationInvitationFilterSet,
    OrganizationMembershipFilterSet,
)
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    get_active_organization_membership,
)
from organizations.permissions import (
    IsOrganizationAdmin,
    OrganizationInvitationPermission,
    OrganizationManagementPermission,
)
from organizations.serializers import (
    AcceptInvitationSerializer,
    CurrentMembershipSerializer,
    GoogleServiceAccountWriteSerializer,
    MyMembershipSerializer,
    OrganizationInvitationSerializer,
    OrganizationMembershipSerializer,
    OrganizationSerializer,
    ServiceAccountReadSerializer,
    ServiceAccountWriteSerializer,
)
from organizations.services import OrganizationService


logger = logging.getLogger(__name__)


class OrganizationViewSet(NoListVintaScheduleModelViewSet):
    """
    A viewset for managing organizations.
    """

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = (IsAuthenticated, OrganizationManagementPermission)
    #: The ``mine`` action lists the caller's own memberships and must not require
    #: the ``X-Organization-Id`` header — it is the endpoint the frontend uses to
    #: *discover* which org ids are available.
    #:
    #: The ``create`` action is also exempt so that a member with existing
    #: memberships can POST /organizations/ without a header (Phase 5). Without
    #: this exemption, the multi-org 400 would fire in ``initial()`` before
    #: ``perform_create`` runs, and the post-create re-resolve in
    #: ``CreateModelMixin.create`` would again raise 400 (the user now has one
    #: more membership than before the write).
    #:
    #: All other actions keep the standard header enforcement (400 / 403).
    active_org_optional_actions = ("mine", "create")

    def get_permissions(self):
        """
        Override permissions per action:
        - update / partial_update: admin-only (IsOrganizationAdmin).  An admin
          can only reach their own org because get_queryset is scoped by
          membership, so cross-org attempts return 404.
        - All other actions keep the class-level defaults (IsAuthenticated +
          OrganizationManagementPermission).  As of Phase 5, ``create`` is open
          to any authenticated user (not gated to membership-less users); the
          other default-permission actions (retrieve, destroy) keep the full
          membership gate.
        """
        if self.action in ("update", "partial_update"):
            return [IsAuthenticated(), IsOrganizationAdmin()]
        return super().get_permissions()

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def get_queryset(self):
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            return Organization.objects.filter(id=membership.organization_id)
        return Organization.objects.none()

    def create(self, request, *args, **kwargs):
        """Create a new organization for the authenticated user.

        Overrides ``CreateModelMixin.create`` to handle the post-write refetch
        correctly for members who already have one or more memberships (Phase 5).

        We skip the base mixin's post-write ``_resolve_active_organization`` call
        entirely.  For the ``create`` action — exempted via
        ``active_org_optional_actions = ("mine", "create")`` — that re-resolve
        would leave a multi-org caller with no ``X-Organization-Id`` header
        resolved to ``None``, making ``get_queryset`` return nothing and causing
        the re-fetch to raise ``DoesNotExist`` / 500.

        Instead, after ``perform_create`` we look up the just-created membership
        directly and stash it on the request so the re-fetch (via
        ``get_queryset``) is scoped to the new organization.
        """
        serializer = self.get_create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance

        # Stash the newly-created membership so get_queryset can scope to the
        # new org for the re-fetch below.  The membership was created by
        # create_organization; it exists in the DB at this point.
        new_membership = (
            OrganizationMembership.objects.filter(
                user=request.user,
                organization_id=instance.pk,
                is_active=True,
            )
            .select_related("organization")
            .first()
        )
        # The membership was just created with is_active=True, so this lookup
        # is expected to always succeed.  A None result here would be a hard bug
        # (e.g. a post-create signal deleted the membership), not a graceful
        # fallback — there is no safe org context to recover with.
        if new_membership is None:
            logger.error(
                "Membership lookup returned None immediately after create for org %s — "
                "this is a bug; the response will be misconfigured.",
                instance.pk,
            )
        request.user._active_membership = new_membership  # type: ignore[union-attr]
        request.organization_membership = new_membership  # type: ignore[attr-defined]
        request.organization = (  # type: ignore[attr-defined]
            new_membership.organization if new_membership is not None else None
        )

        # Re-fetch the instance so any annotations/virtual-model fields on
        # OrganizationVirtualModel are populated.  Mirror the base
        # CreateModelMixin.create branch: prefer get_return_queryset() when
        # present so a future override is not silently ignored here.
        if hasattr(self, "get_return_queryset"):
            annotated_instance = self.get_return_queryset().get(pk=instance.pk)
        else:
            annotated_instance = self.get_queryset().get(pk=instance.pk)
        return_serializer = self.get_retrieve_serializer(annotated_instance)
        headers = self.get_success_headers(return_serializer.data)
        return Response(return_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        """Override update to:

        1. Upsert the org's ``GoogleCalendarServiceAccount`` when ``google_service_account``
           is present in the request body (create-or-update, one per org, calendar FK=None).
        2. Trigger rooms sync when ``should_sync_rooms`` flips False→True — but only when a
           service account is configured (either already stored or just provided in this PATCH).
           If the flag is being enabled and no service account is configured (neither stored
           nor in the request), return **400** so the admin knows to configure first.

        Uses select_for_update to lock the row during snapshot + write, serializing
        concurrent PATCHes and preventing double-fire of the sync on False→True transition.

        The creds check is performed BEFORE any write so that unrelated field changes (e.g.
        renaming the org) are NOT persisted when the 400 is returned.
        """
        partial = kwargs.get("partial", False)

        # Validate the nested google_service_account block if present, before entering the lock.
        sa_data: dict | None = None
        raw_sa = request.data.get("google_service_account")
        if raw_sa is not None:
            sa_serializer = GoogleServiceAccountWriteSerializer(data=raw_sa)
            sa_serializer.is_valid(raise_exception=True)
            sa_data = sa_serializer.validated_data

        # Lock the row during snapshot + write.
        with transaction.atomic():
            instance = Organization.objects.select_for_update().get(pk=self.get_object().pk)
            old_should_sync_rooms = instance.should_sync_rooms

            serializer = self.get_update_serializer(instance, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)

            # Compute desired transition BEFORE writing so we can reject early.
            desired_should_sync_rooms = serializer.validated_data.get(
                "should_sync_rooms", old_should_sync_rooms
            )
            fire = (not old_should_sync_rooms) and desired_should_sync_rooms

            # Guard: enabling sync without any service account → 400 BEFORE any write.
            if fire and sa_data is None:
                has_existing_sa = (
                    GoogleCalendarServiceAccount.objects.filter_by_organization(instance.id)
                    .filter(calendar_fk__isnull=True)
                    .exists()
                )
                if not has_existing_sa:
                    raise NoServiceAccountConfiguredError()

            self.perform_update(serializer)

            # Upsert the service account if provided.
            if sa_data is not None:
                GoogleCalendarServiceAccount.objects.filter_by_organization(instance.id).filter(
                    calendar_fk__isnull=True
                ).delete()
                GoogleCalendarServiceAccount.objects.create(
                    organization=instance,
                    calendar_fk=None,
                    email=sa_data["email"],
                    audience=sa_data["audience"],
                    public_key=sa_data["public_key"],
                    private_key_id=sa_data["private_key_id"],
                    private_key=sa_data["private_key"],
                )

        # Call request_rooms_sync directly — the service now owns the on_commit
        # deferral internally, so no view-level on_commit wrap is needed.
        if fire:
            try:
                self.organization_service.request_rooms_sync(
                    organization=serializer.instance,
                    requested_by=request.user,
                )
            except NoServiceAccountConfiguredError:
                logger.warning(
                    "rooms-sync trigger skipped: no service account configured for org %s "
                    "(account may have been deleted between pre-flight check and commit)",
                    serializer.instance.id,
                )

        return_serializer = self.get_retrieve_serializer(
            self.get_return_object(serializer.instance)
        )
        return Response(return_serializer.data)

    @extend_schema(
        summary="Current organization + role for the authenticated user",
        responses={
            200: CurrentMembershipSerializer,
            404: OpenApiResponse(description="No organization membership (gated user)"),
        },
    )
    @action(detail=False, methods=["get"], url_path="current", permission_classes=[IsAuthenticated])
    def current(self, request):
        """Return the caller's organization and role.

        HTTP 200 — the user is onboarded (has a membership).
        HTTP 404 — the user is gated (no membership yet).
        """
        membership = get_active_organization_membership(request.user)
        if membership is None:
            raise NotFound(detail="No organization membership.")
        serializer = CurrentMembershipSerializer(membership, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="List the authenticated user's active organization memberships",
        responses={
            200: MyMembershipSerializer(many=True),
        },
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="mine",
        permission_classes=[IsAuthenticated],
        pagination_class=None,  # bare list — no count/next/previous envelope
    )
    def mine(self, request):
        """Return all active memberships for the authenticated caller.

        Designed for the frontend org switcher: the client calls this endpoint
        *before* it knows which ``X-Organization-Id`` to send, so no header is
        required.  The response is always HTTP 200; gated users receive an empty
        list (``[]``).
        """
        memberships = OrganizationMembership.objects.active_for_user(request.user)
        serializer = MyMembershipSerializer(memberships, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Trigger a rooms/resources import for the organization",
        responses={
            202: OrganizationSerializer,
            400: OpenApiResponse(description="Invalid datetime format"),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Organization not found"),
        },
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="sync-rooms",
        permission_classes=[IsOrganizationAdmin],
    )
    def sync_rooms(self, request, pk=None):
        """POST /organizations/{id}/sync-rooms/ — enqueue a calendar resources import.

        Optional body fields:
        - ``start_time``: ISO 8601 datetime for the import window start.
        - ``end_time``: ISO 8601 datetime for the import window end.

        Defaults (when omitted): ``start_time=now``, ``end_time=now+365d``.
        Returns HTTP 202 on success.
        """
        org = self.get_object()

        # Parse optional ISO datetime fields from the request body.
        start_time: datetime.datetime | None = None
        end_time: datetime.datetime | None = None

        raw_start = request.data.get("start_time")
        raw_end = request.data.get("end_time")

        try:
            if raw_start:
                start_time = datetime.datetime.fromisoformat(raw_start)
            if raw_end:
                end_time = datetime.datetime.fromisoformat(raw_end)
        except (ValueError, TypeError) as exc:
            raise ValidationError({"detail": f"Invalid datetime format: {exc}"}) from exc

        # Pre-flight: refuse early (400) if no service account is configured so the
        # admin gets a clear error instead of a 500.
        has_sa = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .exists()
        )
        if not has_sa:
            raise NoServiceAccountConfiguredError()

        # Call request_rooms_sync directly — the service now owns the on_commit
        # deferral internally, so no view-level on_commit wrap is needed.
        # Keep the TOCTOU guard in case the SA is deleted between pre-flight and here.
        try:
            self.organization_service.request_rooms_sync(
                organization=org,
                requested_by=request.user,
                start_time=start_time,
                end_time=end_time,
            )
        except NoServiceAccountConfiguredError:
            logger.warning(
                "rooms-sync trigger skipped: no service account configured for org %s "
                "(account may have been deleted between pre-flight check and commit)",
                org.id,
            )

        serializer = self.get_serializer(org)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Trigger a sync of every calendar in the organization",
        request=CalendarSyncRequestSerializer,
        responses={
            202: OpenApiResponse(
                description=(
                    "Sync enqueued. Body: {synced: [calendar_id, ...], "
                    "skipped: [{calendar_id, reason}, ...]}."
                )
            ),
            400: OpenApiResponse(description="Invalid sync window"),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Organization not found"),
        },
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="sync-calendars",
        permission_classes=[IsOrganizationAdmin],
    )
    def sync_calendars(self, request, pk=None):
        """POST /organizations/{id}/sync-calendars/ — enqueue a sync of all calendars.

        Each active calendar in the organization is synced using its owner's
        linked account. Calendars without an owner or a linked provider account
        are reported under ``skipped`` rather than failing the whole request.

        Body (``CalendarSyncRequestSerializer``): ``start_datetime``,
        ``end_datetime`` (required ISO 8601) and ``should_update_events``.
        Returns HTTP 202 with ``{"synced": [...], "skipped": [...]}``.
        """
        org = self.get_object()

        input_serializer = CalendarSyncRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        result = self.organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=request.user,
            start_datetime=data["start_datetime"],
            end_datetime=data["end_datetime"],
            should_update_events=data["should_update_events"],
        )
        return Response(result, status=status.HTTP_202_ACCEPTED)


class ServiceAccountViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Admin-only CRUD for the organization's Google Calendar service account.

    Manages **only** the org-level service account (``calendar_fk IS NULL``) — the
    one used for rooms sync. Per-calendar service accounts are auto-assigned by the
    calendar auth flow and are intentionally not exposed here.

    Secrets (``private_key``, ``private_key_id``) are write-only and never echoed;
    all responses use ``ServiceAccountReadSerializer``. There is at most one
    org-level account per organization: ``create`` refuses a duplicate (rotate via
    PUT/PATCH or DELETE first). Cross-org ids resolve to 404 via the org-scoped
    queryset; non-admins get 403; anonymous requests 401.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = ServiceAccountReadSerializer

    def get_queryset(self):  # type: ignore[override]
        """Org-scoped queryset limited to the org-level service account."""
        user = self.request.user
        if not user.is_authenticated:
            return GoogleCalendarServiceAccount.objects.none()
        membership = get_active_organization_membership(user)
        if membership is None:
            return GoogleCalendarServiceAccount.objects.none()
        return GoogleCalendarServiceAccount.objects.filter_by_organization(
            membership.organization_id
        ).filter(calendar_fk__isnull=True)

    def get_serializer_class(self):  # type: ignore[override]
        if self.action in ("create", "update", "partial_update"):
            return ServiceAccountWriteSerializer
        return ServiceAccountReadSerializer

    @extend_schema(
        request=ServiceAccountWriteSerializer,
        responses={201: ServiceAccountReadSerializer},
    )
    def create(self, request, *args, **kwargs):
        """Create the org-level service account (one per organization).

        HTTP 201 with the secret-free representation. HTTP 400 if an org-level
        account already exists (rotate via PUT/PATCH or DELETE first) or the
        payload is invalid.
        """
        membership = get_active_organization_membership(request.user)
        if membership is None:
            # IsOrganizationAdmin already guards this; defensive fallback.
            return Response(
                {"detail": "No active organization membership."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = ServiceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        already_configured = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(membership.organization_id)
            .filter(calendar_fk__isnull=True)
            .exists()
        )
        if already_configured:
            raise ValidationError(
                {
                    "detail": (
                        "A service account is already configured for this organization. "
                        "Use PUT/PATCH to rotate it, or DELETE it first."
                    )
                }
            )

        account = GoogleCalendarServiceAccount.objects.create(
            organization=membership.organization,
            calendar_fk=None,
            **serializer.validated_data,
        )
        return Response(ServiceAccountReadSerializer(account).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=ServiceAccountWriteSerializer,
        responses={200: ServiceAccountReadSerializer},
    )
    def update(self, request, *args, **kwargs):
        """Rotate/update the org-level service account.

        PUT requires all writable fields; PATCH updates the provided subset
        (secrets are retained when omitted). Returns HTTP 200 with the
        secret-free representation.
        """
        partial = kwargs.get("partial", False)
        account = self.get_object()

        serializer = ServiceAccountWriteSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        for field_name, value in serializer.validated_data.items():
            setattr(account, field_name, value)
        account.save()

        return Response(ServiceAccountReadSerializer(account).data, status=status.HTTP_200_OK)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Delete the org-level service account. HTTP 204."""
        account = self.get_object()
        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrganizationInvitationViewSet(NoUpdateVintaScheduleModelViewSet):
    """
    A viewset for managing organization invitations.
    """

    queryset = OrganizationInvitation.objects.all()
    serializer_class = OrganizationInvitationSerializer
    permission_classes = (OrganizationInvitationPermission,)
    filterset_class = OrganizationInvitationFilterSet

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def get_queryset(self):
        """Filter invitations by the user's organization."""
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            return OrganizationInvitation.objects.filter(organization_id=membership.organization_id)
        # Return empty queryset for users without an active membership
        return OrganizationInvitation.objects.none()

    def get_serializer_context(self):
        """Add organization to serializer context."""
        context = super().get_serializer_context()
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            context["organization"] = membership.organization
        return context

    def perform_destroy(self, instance):
        """Revoke invitation by calling the service method."""
        self.organization_service.revoke_invitation(str(instance.id))

    @extend_schema(
        summary="Resend a pending organization invitation",
        responses={
            200: OrganizationInvitationSerializer,
            400: OpenApiResponse(description="Invitation already accepted or service error"),
            403: OpenApiResponse(description="Not an active member"),
            404: OpenApiResponse(description="Invitation not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="resend")
    def resend(self, request, pk=None):
        """POST /invitations/{id}/resend/ — regenerate token and re-send a pending invitation.

        Guards:
        - Invitation must not be accepted (accepted_at is None).
        - User must be an active member of the invitation's organization.

        Returns the re-serialized invitation with the new token_hash and extended expires_at.
        """
        invitation = self.get_object()  # org-scoped; raises 404 if cross-org

        # Guard: refuse if invitation is already accepted
        if invitation.accepted_at is not None:
            raise ValidationError(detail="Invitation already accepted.")

        # Resolve the requesting user's organization (mirror how the viewset resolves context)
        membership = get_active_organization_membership(request.user)
        if membership is None:
            # This shouldn't happen because OrganizationInvitationPermission.has_permission
            # already checked for active membership, but guard for clarity
            raise PermissionDenied(detail="No active organization membership.")

        # Call the service to reset token+expiry and re-send the email
        invitation = self.organization_service.invite_user_to_organization(
            email=invitation.email,
            first_name=invitation.first_name,
            last_name=invitation.last_name,
            invited_by=request.user,
            organization=membership.organization,
        )

        # Return the re-serialized invitation
        serializer = self.get_serializer(invitation)
        return Response(serializer.data, status=status.HTTP_200_OK)


class OrganizationMembershipViewSet(ReadOnlyVintaScheduleModelViewSet):
    """
    A viewset for listing, retrieving, and managing organization members.

    Admin-only endpoint — lists both active and inactive members of the caller's
    organization, suitable for a datatable view. Non-admin members get 403.

    Actions:
    - `deactivate`: POST to disable a member (prevent self-deactivation and
      protect the last active admin).
    - `reactivate`: POST to re-enable a member.
    """

    queryset = OrganizationMembership.objects.select_related("user", "user__profile")
    serializer_class = OrganizationMembershipSerializer
    permission_classes = (IsOrganizationAdmin,)
    filterset_class = OrganizationMembershipFilterSet

    def get_queryset(self):
        """Org-scoped queryset: return members of the caller's organization only."""
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            return (
                OrganizationMembership.objects.filter(organization_id=membership.organization_id)
                .select_related("user", "user__profile")
                .order_by("id")
            )
        return OrganizationMembership.objects.none()

    @extend_schema(
        summary="Deactivate an organization member",
        responses={
            200: OrganizationMembershipSerializer,
            400: OpenApiResponse(description="Cannot deactivate self or last active admin"),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        """Deactivate a member (set is_active=False).

        Guards:
        - Cannot deactivate own membership (self-lockout prevention).
        - Cannot deactivate the last active admin (org lockout prevention).

        Idempotency: deactivating an already-inactive member is a no-op success.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission
        user = request.user

        # Guard: prevent self-deactivation
        if target.user_id == user.id:
            raise PermissionDenied(detail="Cannot deactivate your own membership.")

        # Guard: prevent deactivating the last active admin (defense-in-depth).
        # This guard is currently unreachable via this endpoint because the requester
        # must be an active admin of the org (IsOrganizationAdmin), and the self-lockout
        # guard above blocks the only path that could drop the org to zero admins
        # (requester attempting to deactivate themselves). Retained to protect any future
        # non-self deactivation paths (e.g., bulk action or service-layer call).
        if target.is_admin:
            org_id = target.organization_id
            other_active_admin_count = (
                OrganizationMembership.objects.filter(
                    organization_id=org_id,
                    role=target.role,  # Same role filter (ADMIN)
                    is_active=True,
                )
                .exclude(id=target.id)  # Exclude the target itself
                .count()
            )
            if other_active_admin_count == 0:
                raise ValidationError(
                    detail="Cannot deactivate the last active admin of the organization."
                )

        # Deactivate (idempotent: no-op if already inactive)
        target.is_active = False
        target.save(update_fields=["is_active"])

        # Return the updated membership
        serializer = self.get_serializer(target)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Reactivate an organization member",
        responses={
            200: OrganizationMembershipSerializer,
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None):
        """Reactivate a member (set is_active=True).

        No guards — re-enabling is always safe.

        Idempotency: reactivating an already-active member is a no-op success.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission

        # Reactivate (idempotent: no-op if already active)
        target.is_active = True
        target.save(update_fields=["is_active"])

        # Return the updated membership
        serializer = self.get_serializer(target)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AcceptInvitationView(generics.CreateAPIView):
    """
    Public endpoint for accepting organization invitations.
    """

    serializer_class = AcceptInvitationSerializer
    permission_classes = (IsAuthenticated,)

    def create(self, request, *args, **kwargs):
        """Accept invitation and return success response.

        Phase 4: ``UserAlreadyHasMembershipError`` now means the caller is already a
        member of *this specific organization* (same-org duplicate), not of any
        organization.  A user in org A who accepts a valid invitation from org B will
        receive 201 and end up with two active memberships.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            membership = serializer.create(serializer.validated_data)
        except UserAlreadyHasMembershipError:
            # 400 — user is already a member of the invitation's organization.
            return Response(
                {"error": UserAlreadyHasMembershipError.default_detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InvalidInvitationTokenError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DuplicateInvitationError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_409_CONFLICT,
            )
        except InvitationNotFoundError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "message": "Invitation accepted successfully",
                "organization_id": membership.organization_id,
                "organization_name": membership.organization.name,
            },
            status=status.HTTP_201_CREATED,
        )

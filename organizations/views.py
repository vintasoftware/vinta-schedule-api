import datetime
import logging
from typing import Annotated, Any, ClassVar, cast

from django.db import transaction
from django.http import FileResponse

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import (
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, serializers, status, views
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from audit.constants import AuditAction
from audit.diff import compute_diff
from audit.services import AuditService
from calendar_integration.models import GoogleCalendarServiceAccount
from calendar_integration.serializers import CalendarSyncRequestSerializer
from common.media_storage_backend import MediaStorage
from common.utils.view_utils import (
    NoListVintaScheduleModelViewSet,
    NoUpdateVintaScheduleModelViewSet,
    ReadOnlyVintaScheduleModelViewSet,
    TenantScopedViewMixin,
)
from organizations.authorization import has_organization_permission
from organizations.branding_logo import (
    BRANDING_LOGO_KEY_PREFIX,
    DEFAULT_LOGO_ASSET_PATH,
    DEFAULT_LOGO_CONTENT_TYPE,
    DEFAULT_LOGO_ETAG_IDENTITY,
    DEFAULT_LOGO_SLUG_SENTINEL,
    LOGO_CACHE_MAX_AGE_SECONDS,
    branding_diff_state,
    compute_logo_etag,
    guess_logo_content_type,
    sign_branding_logo_upload,
)
from organizations.exceptions import (
    BrandingLogoUploadRejectedError,
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
    OrganizationBranding,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    resolve_branding_for_display,
)
from organizations.permission_catalog import (
    MANAGE_MEMBERS,
    membership_state_for_groups,
    permissions_for_groups,
)
from organizations.permissions import (
    BRANDING_GATE_EXCEPTIONS,
    BrandingWriteGateReason,
    IsOrganizationAdmin,
    OrganizationInvitationPermission,
    OrganizationManagementPermission,
    check_branding_read_eligibility,
    evaluate_branding_write_gate,
)
from organizations.serializers import (
    AcceptInvitationSerializer,
    AssignMembershipGroupsSerializer,
    CurrentMembershipSerializer,
    GoogleServiceAccountWriteSerializer,
    MyMembershipSerializer,
    OrganizationBrandingLogoUploadParamsRequestSerializer,
    OrganizationBrandingLogoUploadParamsSerializer,
    OrganizationBrandingSerializer,
    OrganizationInvitationSerializer,
    OrganizationMembershipSerializer,
    OrganizationSerializer,
    ServiceAccountReadSerializer,
    ServiceAccountWriteSerializer,
)
from organizations.services import OrganizationService, sync_membership_groups_from_role
from payments.services.entitlement_service import EntitlementService


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
    #: memberships can POST /organizations/ without a header. Without
    #: this exemption, the multi-org 400 would fire in ``perform_authentication()``
    #: before ``perform_create`` runs, and the post-create re-resolve in
    #: ``CreateModelMixin.create`` would again raise 400 (the user now has one
    #: more membership than before the write).
    #:
    #: All other actions keep the standard header enforcement (400 / 403).
    organization_optional_actions = ("mine", "create")

    def get_permissions(self):
        """
        Override permissions per action:
        - update / partial_update: admin-only (IsOrganizationAdmin).  An admin
          can only reach their own org because get_queryset is scoped by
          membership, so cross-org attempts return 404.
        - All other actions keep the class-level defaults (IsAuthenticated +
          OrganizationManagementPermission).  ``create`` is open
          to any authenticated user (not restricted to membership-less users); the
          other default-permission actions (retrieve, destroy) keep the full
          membership check.
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
        membership = self.request.organization_membership
        if membership:
            return Organization.objects.filter(id=membership.organization_id)
        return Organization.objects.none()

    def create(self, request, *args, **kwargs):
        """Create a new organization for the authenticated user.

        Overrides ``CreateModelMixin.create`` to handle the post-write refetch
        correctly for members who already have one or more memberships.

        We skip the base mixin's post-write ``resolve_organization`` call
        entirely.  For the ``create`` action — exempted via
        ``organization_optional_actions = ("mine", "create")`` — that re-resolve
        would leave a multi-org caller with no ``X-Organization-Id`` header
        resolved to ``None``, making ``get_queryset`` return nothing and causing
        the re-fetch to raise ``DoesNotExist`` / 500.

        Instead, after ``perform_create`` we look up the just-created membership
        directly and stash it on the request so the re-fetch (via
        ``get_queryset``) is scoped to the new organization -- and bind that
        organization to the context, which is the half of the base mixin's
        re-resolve that still applies here.
        """
        serializer = self.get_create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance

        # Put the newly-created membership on the request so get_queryset can scope to the
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
        request.organization_membership = new_membership  # type: ignore[attr-defined]
        request.organization = (  # type: ignore[attr-defined]
            new_membership.organization if new_membership is not None else None
        )
        # ...and bind what was just resolved. Skipping the base mixin's
        # post-create ``resolve_organization`` also skips its re-bind, so
        # without this line the context stays on whatever ``initial()`` resolved
        # -- ``None`` for a first-time creator, or organization A for an existing
        # member who sent ``X-Organization-Id: A`` while creating organization B
        # -- while every line below reads the *new* organization off the request.
        # The re-fetch and the serializer would then run through default managers
        # scoped to a different organization than the one being returned.
        self.bind_organization(request.organization)  # type: ignore[attr-defined]

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
                # A RESTRICTED organization may not write, and the service-account
                # upsert below is a real user-initiated write on an
                # organization-scoped model (``GoogleCalendarServiceAccount``) --
                # block it here, the same check every other blocked write consults.
                self.organization_service.entitlement_service.check_not_restricted(instance)
                GoogleCalendarServiceAccount.objects.filter_by_organization(instance.id).filter(
                    calendar_fk__isnull=True
                ).delete()
                GoogleCalendarServiceAccount.objects.create(
                    organization=instance,
                    calendar_fk=None,
                    email=sa_data["email"],
                    admin_email=sa_data["admin_email"],
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
        summary="Current organization + permissions for the authenticated user",
        responses={
            200: CurrentMembershipSerializer,
            404: OpenApiResponse(description="No organization membership (gated user)"),
        },
    )
    @action(detail=False, methods=["get"], url_path="current", permission_classes=[IsAuthenticated])
    def current(self, request):
        """Return the caller's organization and the capabilities they hold in it.

        HTTP 200 — the user is onboarded (has a membership).
        HTTP 404 — the user is gated (no membership yet).
        """
        membership = request.organization_membership
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


class ServiceAccountViewSet(
    TenantScopedViewMixin, ListModelMixin, RetrieveModelMixin, GenericViewSet
):
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

    @inject
    def __init__(
        self,
        *args,
        entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.entitlement_service = entitlement_service

    def get_queryset(self):  # type: ignore[override]
        """Org-scoped queryset limited to the org-level service account."""
        user = self.request.user
        if not user.is_authenticated:
            return GoogleCalendarServiceAccount.objects.none()
        membership = self.request.organization_membership
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
        membership = request.organization_membership
        if membership is None:
            # IsOrganizationAdmin already guards this; defensive fallback.
            return Response(
                {"detail": "No active organization membership."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # A RESTRICTED organization may not write, including provisioning a service
        # account -- the same check every other blocked write consults.
        self.entitlement_service.check_not_restricted(membership.organization)

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

        # A RESTRICTED organization may not rotate/update its service account.
        # ``partial_update`` routes through this method, so both
        # PUT and PATCH are covered here.
        self.entitlement_service.check_not_restricted(account.organization)

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

        # A RESTRICTED organization may not delete its service account -- the same
        # check every other blocked write consults.
        self.entitlement_service.check_not_restricted(account.organization)

        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    create=extend_schema(
        responses={
            201: OrganizationInvitationSerializer,
            402: OpenApiResponse(description="Organization is at its seat limit"),
        },
    ),
)
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
        membership = self.request.organization_membership
        if membership:
            return OrganizationInvitation.objects.filter(organization_id=membership.organization_id)
        # Return empty queryset for users without an active membership
        return OrganizationInvitation.objects.none()

    def get_serializer_context(self):
        """Add organization to serializer context."""
        context = super().get_serializer_context()
        membership = self.request.organization_membership
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
            402: OpenApiResponse(description="Organization is at its seat limit"),
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
        membership = request.organization_membership
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
    - `groups`: POST to set a member's groups, and with them their capabilities
      (protects the last member who can manage members).
    """

    queryset = OrganizationMembership.objects.select_related("user", "user__profile")
    serializer_class = OrganizationMembershipSerializer
    permission_classes = (IsOrganizationAdmin,)
    filterset_class = OrganizationMembershipFilterSet
    # OrganizationMembership has a composite PK (user, organization) and no scalar
    # ``id``. The queryset is already scoped to the caller's single organization, so a
    # member is uniquely identified within that scope by ``user_id``; use it as the
    # detail-route lookup instead of the (now non-existent) scalar pk.
    lookup_field = "user_id"

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
        """Org-scoped queryset: return members of the caller's organization only."""
        membership = self.request.organization_membership
        if membership:
            return (
                OrganizationMembership.objects.filter(organization_id=membership.organization_id)
                .select_related("user", "user__profile")
                # OrganizationMembership has a composite PK (user, organization) and no
                # scalar ``id``; order by the PK columns for a stable, deterministic list.
                .order_by("user_id", "organization_id")
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
    def deactivate(self, request, user_id=None):
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

        # A restricted organization may not write, including deactivating one of
        # its own members. Checked here rather than in ``OrganizationService`` --
        # this write, unlike every other membership write in this module, has never
        # gone through the service layer; moving the whole action there is a larger
        # refactor than this check warrants.
        self.organization_service.entitlement_service.check_not_restricted(target.organization)

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
                # Composite PK (user, organization): exclude the target by its user_id
                # within the already org-scoped filter.
                .exclude(user_id=target.user_id)
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
            402: OpenApiResponse(description="Organization is at its seat limit"),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, user_id=None):
        """Reactivate a member (set is_active=True).

        The seat-limit check lives in ``OrganizationService.reactivate_membership``
        (the service layer, not the viewset), so this
        action only resolves the target and serializes the result. Idempotency:
        reactivating an already-active member is a no-op success.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission

        target = self.organization_service.reactivate_membership(target)

        serializer = self.get_serializer(target)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Set an organization member's groups",
        request=AssignMembershipGroupsSerializer,
        responses={
            200: OrganizationMembershipSerializer,
            400: OpenApiResponse(
                description=(
                    "Unknown group name, empty group list, or the assignment would "
                    "leave the organization with nobody who can manage members"
                )
            ),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="groups")
    def assign_groups(self, request, user_id=None):
        """Replace a member's groups, and with them the capabilities they hold.

        Replaces the former ``update-role`` action. The request names *groups*
        -- the one write where a group name is the natural input, since
        assigning a group is the act of choosing one -- and the response
        reports the resulting *permissions*, which is what every authorization
        check actually reads.

        Guards:

        - **The organization keeps at least one member who can manage
          members.** Restated from "cannot demote the last active admin": the
          rule counts by capability (``organizations.manage_members``) rather
          than by the ``role`` column, so it holds for any future group that
          carries the capability and does not depend on a representation the
          API no longer exposes.
        - A restricted organization may not write at all. See ``deactivate``
          above for why the check is here rather than in
          ``OrganizationService``.

        Idempotency: assigning the groups a member already holds is a no-op
        success, including for the sole administrator re-assigning
        ``organization_admin`` to themselves -- the guard fires on *losing* the
        capability, not on writing it again.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission

        self.organization_service.entitlement_service.check_not_restricted(target.organization)

        serializer = AssignMembershipGroupsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_groups = serializer.validated_data["groups"]

        # Guard: the organization must keep somebody who can manage members.
        #
        # Asked in three parts, in the cheap-first order: does the assignment
        # *remove* the capability (no query at all), did the target actually
        # hold it (resolved the same way every permission class resolves it),
        # and is anybody else left holding it.
        keeps_manage_members = MANAGE_MEMBERS in permissions_for_groups(requested_groups)
        if not keeps_manage_members and has_organization_permission(
            target.user, MANAGE_MEMBERS, target.organization
        ):
            others_who_can_manage_members = (
                OrganizationMembership.objects.filter(
                    organization_id=target.organization_id,
                    is_active=True,
                )
                # A membership is identified by ``(user, organization)``; the
                # filter above is already organization-scoped.
                .exclude(user_id=target.user_id)
                .holding_permission(MANAGE_MEMBERS)
                .count()
            )
            if others_who_can_manage_members == 0:
                raise ValidationError(
                    detail=(
                        "Cannot remove the last active member who can manage members "
                        "from the organization."
                    )
                )

        # TEMPORARY DUAL-WRITE, deleted in Phase 6 with the two columns. The
        # write goes through ``role`` / ``is_billing_owner`` and then back out
        # through ``sync_membership_groups_from_role`` rather than straight to
        # ``target.groups``, for two reasons: the columns are still read
        # outside the permission classes (``public_api.scoping``,
        # ``calendar_integration``), so leaving them stale would authorize the
        # member differently depending on which reader asked; and routing
        # through the same shim every other membership write uses is what
        # *canonicalises* the stored group set, so no request body can persist
        # a combination the shim would later overwrite.
        is_admin, is_billing_owner = membership_state_for_groups(requested_groups)
        target.role = OrganizationRole.ADMIN if is_admin else OrganizationRole.MEMBER
        target.is_billing_owner = is_billing_owner
        target.save(update_fields=["role", "is_billing_owner"])
        sync_membership_groups_from_role(target)

        # Return the updated membership
        read_serializer = self.get_serializer(target)
        return Response(read_serializer.data, status=status.HTTP_200_OK)


class AcceptInvitationView(generics.CreateAPIView):
    """
    Public endpoint for accepting organization invitations.
    """

    serializer_class = AcceptInvitationSerializer
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        responses={
            # `create` below returns {"message", "organization_id", "organization_name"},
            # not an AcceptInvitationSerializer instance — describe the actual body.
            201: OpenApiResponse(
                response=inline_serializer(
                    name="AcceptInvitationResponse",
                    fields={
                        "message": serializers.CharField(),
                        "organization_id": serializers.IntegerField(),
                        "organization_name": serializers.CharField(),
                    },
                ),
                description="Invitation accepted",
            ),
            400: OpenApiResponse(description="Already a member, or invalid token"),
            402: OpenApiResponse(description="Organization is at its seat limit"),
            404: OpenApiResponse(description="Invitation not found"),
            409: OpenApiResponse(description="Duplicate invitation"),
        },
    )
    def post(self, request, *args, **kwargs):
        # drf-spectacular resolves a plain APIView's schema from the HTTP-verb
        # method (`post`), not from `create` -- see OrganizationBrandingView's
        # get/put/patch above for the same convention. `create` stays the
        # override point (it holds the actual logic) since that is the DRF
        # ``CreateAPIView`` convention every caller of this class expects.
        return self.create(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """Accept invitation and return success response.

        ``UserAlreadyHasMembershipError`` means the caller is already a
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


@extend_schema(tags=["Branding"])
class OrganizationBrandingView(TenantScopedViewMixin, views.APIView):
    """Admin-only REST endpoint for managing a parentless, entitled
    organization's branding.

    Write gate (Organization Auth-Area Branding plan, Phase 3): PUT/PATCH
    require the acting org to be parentless and hold the
    ``white_label_branding`` entitlement
    (``organizations.permissions.evaluate_branding_write_gate`` -- its third,
    slug-set condition is retired, see that function), AND the caller must be
    an org admin (``IsOrganizationAdmin`` permission). Replaces the earlier
    reseller-only gate (``is_reseller()``) -- any paying, parentless
    organization can now manage its own branding, not just resellers. Each of
    the two failure conditions raises its own ``PermissionDenied`` subclass
    (``organizations.exceptions``) so the response body -- not just the 403
    status -- distinguishes the permanent refusal (has a parent) from the
    billing state (not entitled).

    GET uses the **eligibility** gate
    (``organizations.permissions.is_branding_eligible_organization`` --
    parentless AND entitled, via ``_check_branding_read_gate``). It admits
    exactly what the write gate admits; the two are kept separate because they
    answer different questions and a future condition may again apply to only
    one of them (see ``evaluate_branding_write_gate``). The read path is routed
    through the eligibility gate rather than the write gate, keeping this
    endpoint consistent with the ``can_manage_branding`` contract an eligible
    org's SPA would otherwise render into a page that immediately 403s.
    GET refuses with the same parent/entitlement 403 bodies as the write gate.

    Operations: retrieve (GET) + upsert (PUT/PATCH) the **acting org's own**
    branding. The endpoint operates on the request's organization only — it
    cannot brand another org's tree. A GET with no row returns 404.

    Round-trips: app_name, logo_url, primary_color, secondary_color,
    support_email, redirect_url. NEVER exposes can_invite_organizations
    or makes organization writable.

    Tenant-scoping: ``TenantScopedViewMixin`` resolves ``X-Organization-Id``
    before any handler runs, so multi-org admins always operate on the
    header-named org. Without the header a multi-org caller receives 400;
    a header naming a non-member org receives 403 — identical to every other
    org-scoped endpoint. ``OrganizationScopedAPIViewMixin
    .is_organization_resolution_optional`` uses ``getattr(self, "action", None)``,
    so the absent ``self.action`` attribute (ViewSetMixin is not in the MRO) is
    safe — ``None`` is never in the default ``organization_optional_actions = ()``
    tuple, so full 400/403 enforcement runs on every HTTP method.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = OrganizationBrandingSerializer

    @inject
    def __init__(
        self,
        *args,
        audit_service: Annotated[AuditService, Provide["audit_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.audit_service = audit_service

    _GATE_EXCEPTIONS: ClassVar[dict[BrandingWriteGateReason, type[PermissionDenied]]] = (
        BRANDING_GATE_EXCEPTIONS
    )

    def _check_branding_write_gate(self) -> None:
        """Verify the acting org passes the write gate (parentless and
        entitled -- its third, slug-set condition is retired; see
        ``organizations.permissions.evaluate_branding_write_gate``). Raises the
        matching ``PermissionDenied`` subclass on the first failed condition; a
        no-op when the gate admits the org."""
        user = self.request.user
        # Narrows AbstractBaseUser | AnonymousUser -> AbstractBaseUser for mypy
        # (matches the pattern in ServiceAccountViewSet.get_queryset above);
        # IsOrganizationAdmin already blocks anonymous callers before this runs.
        if not user.is_authenticated:
            raise PermissionDenied("No active organization membership.")
        membership = cast("Any", self.request).organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")
        reason = evaluate_branding_write_gate(membership.organization)
        if reason is BrandingWriteGateReason.OK:
            return
        raise self._GATE_EXCEPTIONS[reason]()

    def _check_branding_read_gate(self) -> None:
        """Verify the acting org passes the two-condition branding
        **eligibility** gate (parentless, entitled) used for GET.

        Delegates to ``organizations.permissions.check_branding_read_eligibility``,
        shared with ``OrganizationBrandingLogoUploadParamsView``. That gate used
        to admit one reason more than the write gate (``NO_SLUG``, retired in
        Phase 1 and deleted in Phase 4); the two now admit the same set, and the
        split is kept only because they answer different questions."""
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("No active organization membership.")
        membership = cast("Any", self.request).organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")
        check_branding_read_eligibility(membership.organization)

    def _get_branding_or_404(self):
        """Get the acting org's branding, or raise 404 if not set."""
        membership = self.request.organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")

        try:
            return OrganizationBranding.objects.get(organization_id=membership.organization_id)
        except OrganizationBranding.DoesNotExist as e:
            raise NotFound("Branding not yet configured for this organization.") from e

    @extend_schema(
        summary="Retrieve the acting organization's branding",
        responses={
            200: OrganizationBrandingSerializer,
            403: OpenApiResponse(
                description="Organization has a parent or lacks the entitlement; or not an admin."
            ),
            404: OpenApiResponse(description="Branding not yet configured"),
        },
    )
    def get(self, request, *args, **kwargs):
        """GET /branding/ — retrieve the acting org's branding.

        Uses the two-condition eligibility gate (parentless, entitled) rather
        than the write gate; the two admit the same set -- see
        ``_check_branding_read_gate``. An eligible org with no branding row yet
        falls through to the 404-no-row-yet / 200-with-a-row branch below."""
        self._check_branding_read_gate()
        instance = self._get_branding_or_404()
        serializer = OrganizationBrandingSerializer(instance, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Create or replace the acting organization's branding",
        request=OrganizationBrandingSerializer,
        responses={
            201: OrganizationBrandingSerializer,
            200: OrganizationBrandingSerializer,
            400: OpenApiResponse(description="Invalid input (color format, URL validation)"),
            403: OpenApiResponse(
                description="Organization has a parent or lacks the entitlement; or not an admin"
            ),
        },
    )
    def put(self, request, *args, **kwargs):
        """PUT /branding/ — create or replace the acting org's branding.

        Audited (Organization Auth-Area Branding plan, Phase 4): a refused write
        (gate failure or serializer validation error) raises before this method
        reaches the upsert, so nothing is ever recorded for a refused write. A
        first-time upsert records a CREATE with no diff; an upsert that replaces
        an existing row records an UPDATE with a diff naming only the fields that
        actually changed, using the before-state captured BEFORE the write.
        """
        self._check_branding_write_gate()
        membership = request.organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")

        serializer = OrganizationBrandingSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        existing = OrganizationBranding.objects.filter(
            organization_id=membership.organization_id
        ).first()
        before = branding_diff_state(existing) if existing is not None else None

        # Create or update (upsert) the branding row for the acting org
        instance, created = OrganizationBranding.objects.update_or_create(
            organization_id=membership.organization_id,
            defaults=serializer.validated_data,
        )

        self._record_branding_audit(membership, instance, created=created, before=before)

        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(
            OrganizationBrandingSerializer(instance, context={"request": request}).data,
            status=status_code,
        )

    @extend_schema(
        summary="Update the acting organization's branding (partial)",
        request=OrganizationBrandingSerializer,
        responses={
            200: OrganizationBrandingSerializer,
            400: OpenApiResponse(description="Invalid input (color format, URL validation)"),
            403: OpenApiResponse(
                description="Organization has a parent or lacks the entitlement; or not an admin"
            ),
            404: OpenApiResponse(description="Branding not yet configured"),
        },
    )
    def patch(self, request, *args, **kwargs):
        """PATCH /branding/ — update the acting org's branding (partial).

        Audited (Organization Auth-Area Branding plan, Phase 4): a refused write
        (gate failure, 404-not-configured, or serializer validation error) raises
        before this method reaches ``serializer.save()``, so nothing is ever
        recorded for a refused write. Always an UPDATE (PATCH never creates —
        see ``_get_branding_or_404``); the before-state is captured BEFORE
        ``serializer.save()`` mutates ``instance`` in place.
        """
        self._check_branding_write_gate()
        instance = self._get_branding_or_404()
        before = branding_diff_state(instance)

        serializer = OrganizationBrandingSerializer(
            instance, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        membership = request.organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")
        self._record_branding_audit(membership, instance, created=False, before=before)

        return Response(serializer.data, status=status.HTTP_200_OK)

    def _record_branding_audit(
        self,
        membership: OrganizationMembership,
        instance: OrganizationBranding,
        *,
        created: bool,
        before: dict[str, str] | None,
    ) -> None:
        """Emit the ``AuditService`` CREATE/UPDATE record for a successful
        branding write. Actor is the acting admin's membership (mirrors
        ``OrganizationService.create_organization``'s actor derivation for an
        org-level write). ``UPDATE`` carries a diff naming only the fields that
        changed; ``CREATE`` carries none."""
        actor = self.audit_service.actor_from_membership(membership)
        subject = self.audit_service.subject_from_instance(instance, label=instance.app_name)
        if created:
            self.audit_service.record(
                organization_id=membership.organization_id,
                action=AuditAction.CREATE,
                actor=actor,
                subject=subject,
            )
            return

        after = branding_diff_state(instance)
        diff = compute_diff(before or {}, after)
        self.audit_service.record(
            organization_id=membership.organization_id,
            action=AuditAction.UPDATE,
            actor=actor,
            subject=subject,
            diff=diff,
        )


@extend_schema(tags=["Branding"])
class OrganizationBrandingLogoUploadParamsView(TenantScopedViewMixin, views.APIView):
    """Signs a ``branding_logos`` S3 upload for the acting organization's admin.

    The shipped ``django-s3direct`` signing view (``POST
    /s3direct/get_upload_params/``) is a plain Django view authenticated only
    by session cookie -- it never reaches DRF's ``JWTAuthentication``, so the
    JWT-only frontend SPA gets ``AnonymousUser`` there and is refused
    unconditionally. This view is the REST sibling of the GraphQL
    ``create_branding_logo_upload`` mutation (``public_api.mutations.Mutation``),
    reusing the same ``sign_branding_logo_upload`` signing helper so the S3
    key/credential logic has one implementation.

    Gated on the branding **eligibility** check
    (``organizations.permissions.check_branding_read_eligibility`` -- parentless
    AND entitled) rather than on the write gate. The two admit the same set now
    that the write gate's slug condition is retired, but the split is kept: the
    frontend uploads a logo on file-picker change, before the branding PUT on
    form submit, so this surface deliberately depends on the read-side gate.
    Matches ``OrganizationBrandingView.get``'s read gate.
    """

    permission_classes = (IsOrganizationAdmin,)

    @extend_schema(
        summary="Sign a branding logo upload",
        request=OrganizationBrandingLogoUploadParamsRequestSerializer,
        responses={
            200: OrganizationBrandingLogoUploadParamsSerializer,
            400: OpenApiResponse(description="Disallowed content type or file size"),
            403: OpenApiResponse(
                description="Organization has a parent or lacks the entitlement; or not an admin"
            ),
        },
    )
    def post(self, request, *args, **kwargs):
        membership = request.organization_membership
        if membership is None:
            raise PermissionDenied("No active organization membership.")
        check_branding_read_eligibility(membership.organization)

        request_serializer = OrganizationBrandingLogoUploadParamsRequestSerializer(
            data=request.data
        )
        request_serializer.is_valid(raise_exception=True)

        try:
            payload = sign_branding_logo_upload(**request_serializer.validated_data)
        except BrandingLogoUploadRejectedError as e:
            raise ValidationError(str(e)) from e

        serializer = OrganizationBrandingLogoUploadParamsSerializer(payload)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema(tags=["Branding"])
class OrganizationLogoDeliveryView(views.APIView):
    """Unauthenticated route streaming an organization's branding logo image.

    Keyed on the organization's public ``slug`` (a path segment), never on an S3
    object key: resolves slug -> ``Organization`` -> ``resolve_branding_for_display``
    -> the branding row's stored ``logo`` key, then streams that S3 object. Because
    resolution only ever reaches an object through a branding row, this route
    cannot be pointed at an arbitrary key.

    Every miss -- an unknown slug, an ``Organization`` with no branding row, a
    branding row with no logo, or an organization that lost the
    ``white_label_branding`` entitlement -- streams the same bundled default logo
    along the identical path, with an identical response shape (status, headers,
    caching), so this route answers no question about which organizations exist
    or are branded (matches ``brandingForTenant``'s no-enumeration-oracle contract).

    Resolution always goes through ``resolve_branding_for_display``, so this route
    automatically inherits the widened branding root once Phase 5 of the
    Organization Auth-Area Branding plan lands ``get_branding_root``'s parentless
    case -- no second change here.

    ``Cache-Control`` carries a short max-age and the ``ETag`` is derived from the
    stored key (or a fixed sentinel for the default logo): the route's URL is
    stable across re-uploads, so a long max-age would pin a replaced logo in
    caches and in already-delivered emails.

    That stable URL is exactly why the API read surfaces no longer use this route:
    ``OrganizationBrandingSerializer`` and ``branding_for_tenant`` hand out signed
    S3 URLs (``organizations.branding_logo.signed_logo_url``), which change with
    the stored key and so cannot be cached past a re-upload. This route remains
    the delivery path for the two cases a signature cannot serve: the invitation
    email (opened long after any signature would expire) and the bundled default
    logo for organizations with none of their own.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)

    @extend_schema(
        summary="Deliver an organization's branding logo (or our default)",
        responses={(200, "image/*"): OpenApiTypes.BINARY},
    )
    def get(self, request, org_slug, *args, **kwargs):
        """GET /branding/logo/<org_slug>/ — stream the resolved logo or the default."""
        key = self._resolve_logo_key(org_slug)
        response = self._stream_key(key) if key else None
        if response is None:
            response = self._stream_default()
        return response

    def _resolve_logo_key(self, org_slug: str) -> str:
        """Resolve ``org_slug`` to a stored S3 key, or ``""`` on any miss.

        Deliberately returns an empty string (never raises, never 404s) for
        every miss condition so the caller's fallback to the default logo is a
        single, uniform branch — see the class docstring on why every miss must
        look identical.

        ``resolve_branding_for_display`` is called unconditionally on every
        non-sentinel slug, whether or not the slug matched an ``Organization``
        row (it accepts ``None`` and returns ``None`` at zero extra query cost).
        Branching around the call for an unknown slug would make "was
        resolution attempted at all" an observable difference (query count) from
        an existing, unbranded organization — the enumeration oracle a caller
        could otherwise use to distinguish "no such org" from "real org, no
        logo" without ever seeing a different response body.
        """
        if org_slug == DEFAULT_LOGO_SLUG_SENTINEL:
            return ""

        organization = Organization.objects.filter(slug=org_slug).first()
        branding = resolve_branding_for_display(organization)
        if branding is None:
            return ""

        logo = branding.logo
        key = (logo.name or "") if logo else ""

        # Defense in depth against BLOCKER 1 (arbitrary-key cross-tenant
        # disclosure): even if a key outside the branding_logos upload prefix
        # somehow ended up on a branding row (bypassing the write-side rejection
        # in `normalize_uploaded_logo_key`, e.g. a row inserted directly), never
        # stream it -- treat it exactly like "no logo configured" instead.
        if key and not key.startswith(BRANDING_LOGO_KEY_PREFIX):
            logger.warning(
                "Refusing to serve branding logo key outside the allowed prefix "
                "for organization slug %s",
                org_slug,
            )
            return ""

        return key

    def _stream_key(self, key: str) -> FileResponse | None:
        """Stream the S3 object at ``key``, or ``None`` if it does not exist.

        Checks existence up front (rather than opening blindly and letting a
        missing-object error surface mid-stream, after headers are already
        sent) so a branding row referencing a since-deleted object degrades
        cleanly to the default logo instead of a broken response.
        """
        storage = MediaStorage()
        try:
            if not storage.exists(key):
                return None
            file_obj = storage.open(key, "rb")
        except Exception:  # noqa: BLE001 -- any storage failure degrades to the
            # default logo, never a 500: this route must never distinguish a
            # transient/backend storage error from "no logo configured".
            logger.warning("Failed to resolve branding logo for key %s", key, exc_info=True)
            return None

        response = FileResponse(file_obj, content_type=guess_logo_content_type(key))
        self._set_cache_headers(response, compute_logo_etag(key))
        return response

    def _stream_default(self) -> FileResponse:
        file_obj = DEFAULT_LOGO_ASSET_PATH.open("rb")
        response = FileResponse(file_obj, content_type=DEFAULT_LOGO_CONTENT_TYPE)
        self._set_cache_headers(response, compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY))
        return response

    def _set_cache_headers(self, response: FileResponse, etag: str) -> None:
        response["Cache-Control"] = f"public, max-age={LOGO_CACHE_MAX_AGE_SECONDS}"
        response["ETag"] = etag
        # BLOCKER 2 (Phase 2b security review): the delivery route must never let
        # a browser sniff the body into a renderable type regardless of the
        # (allowlisted, but still attacker-influenced) Content-Type header --
        # applies to both the real-object stream and the default logo.
        response["X-Content-Type-Options"] = "nosniff"

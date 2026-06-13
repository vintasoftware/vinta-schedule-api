"""
Views for the notifications app.

NotificationViewSet exposes user-scoped in-app notification endpoints.
Auth: IsAuthenticated (JWT/session). User-scoped — no org context required.
"""

import logging
from typing import Annotated

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import NotificationUpdateError
from vintasend.services.notification_service import NotificationService
from vintasend_django.models import Notification

from notifications.serializers import NotificationSerializer


logger = logging.getLogger(__name__)

_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 10
_MAX_PAGE_SIZE = 100


def _parse_positive_int(value: str, param_name: str, default: int) -> int:
    """
    Parse a query-param value as a positive integer.

    Raises ValidationError (HTTP 400) when the value is present but not a
    positive integer. Returns ``default`` when the value is absent or empty.
    """
    if not value:
        return default
    try:
        parsed = int(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError({param_name: f"Must be a positive integer, got {value!r}."}) from exc
    if parsed < 1:
        raise ValidationError({param_name: f"Must be >= 1, got {parsed}."})
    return parsed


@extend_schema(tags=["Notifications"])
class NotificationViewSet(ListModelMixin, GenericViewSet):
    """
    ViewSet for user-scoped in-app notifications.

    Endpoints:
    - GET /notifications/        — list all SENT+READ IN_APP notifications for the
      authenticated user, ordered newest first, paginated via LimitOffsetPagination
      (limit/offset query params; envelope: {count, next, previous, results}).
    - GET /notifications/unread/ — unread (SENT only) IN_APP notifications, fetched via
      the native vintasend get_in_app_unread; paginated via page/page_size passthrough
      (envelope: {results, page, page_size}).

    Authentication: IsAuthenticated (JWT or session).
    Scope: request.user — no org context.
    """

    permission_classes = (IsAuthenticated,)
    serializer_class = NotificationSerializer

    @inject
    def __init__(
        self,
        *args,
        notification_service: Annotated[NotificationService, Provide["notification_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.notification_service = notification_service

    @extend_schema(
        summary="List all in-app notifications for the authenticated user",
        description=(
            "Returns the authenticated user's IN_APP notifications with status in (SENT, READ). "
            "Ordered by creation date (newest first). Paginated via limit/offset."
        ),
        parameters=[
            OpenApiParameter(
                name="limit",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of items per page. Defaults to 10.",
            ),
            OpenApiParameter(
                name="offset",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of items to skip. Defaults to 0.",
            ),
        ],
        responses={
            200: OpenApiResponse(
                description=(
                    "Standard LimitOffsetPagination envelope: "
                    "{count: int, next: url|null, previous: url|null, results: [...]}"
                )
            ),
            401: OpenApiResponse(description="Unauthenticated"),
        },
    )
    def list(self, request: Request, *args, **kwargs) -> Response:  # type: ignore[override]
        """GET /notifications/ — list all in-app notifications for the current user.

        Returns the authenticated user's IN_APP notifications with status in (SENT, READ),
        ordered by creation date (newest first). Uses the project's default
        LimitOffsetPagination (limit/offset query params).
        """
        return super().list(request, *args, **kwargs)

    def get_queryset(self):  # type: ignore[override]
        """
        Return the requesting user's IN_APP notifications (SENT + READ).

        Used by the router for schema introspection and reused by Phase 2's
        list-all and Phase 3's mark-read endpoints.  The unread @action bypasses
        this queryset and calls get_in_app_unread() directly via the service.
        """
        if not self.request.user.is_authenticated:
            return Notification.objects.none()
        return Notification.objects.filter(
            user=self.request.user,
            notification_type=NotificationTypes.IN_APP.value,
            status__in=[NotificationStatus.SENT.value, NotificationStatus.READ.value],
        ).order_by("-created", "-id")

    @extend_schema(
        summary="List unread in-app notifications for the authenticated user",
        parameters=[
            OpenApiParameter(
                name="page",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Page number (1-based). Defaults to 1.",
            ),
            OpenApiParameter(
                name="page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of items per page. Defaults to 10.",
            ),
        ],
        responses={
            200: OpenApiResponse(
                description=(
                    "Passthrough-paginated list of unread notifications. "
                    "Envelope: {results: [...], page: int, page_size: int}."
                )
            ),
            400: OpenApiResponse(description="Invalid page / page_size parameter"),
            401: OpenApiResponse(description="Unauthenticated"),
        },
    )
    @action(detail=False, methods=["get"], url_path="unread")
    def unread(self, request: Request) -> Response:
        """GET /notifications/unread/ — unread in-app notifications for the current user.

        Uses the native vintasend NotificationService.get_in_app_unread(user_id, page,
        page_size) which returns an Iterable of vintasend Notification dataclasses
        (not an ORM queryset).  Passthrough pagination: the caller controls page +
        page_size; no server-side count is available from the native method.
        """
        page = _parse_positive_int(
            request.query_params.get("page", ""),
            param_name="page",
            default=_DEFAULT_PAGE,
        )
        page_size = min(
            _parse_positive_int(
                request.query_params.get("page_size", ""),
                param_name="page_size",
                default=_DEFAULT_PAGE_SIZE,
            ),
            _MAX_PAGE_SIZE,
        )

        # request.user is guaranteed to be authenticated here (IsAuthenticated guard),
        # so user.id is always a non-None int.
        user_id = request.user.id
        if user_id is None:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        notifications = list(self.notification_service.get_in_app_unread(user_id, page, page_size))

        serializer = self.get_serializer(notifications, many=True)
        return Response(
            {
                "results": serializer.data,
                "page": page,
                "page_size": page_size,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Mark a notification as read",
        description=(
            "Marks a single in-app notification as read for the authenticated user. "
            "Ownership is enforced via the scoped queryset — only the owner can mark "
            "their own notifications. Calling this endpoint on an already-READ "
            "notification is idempotent (returns 200)."
        ),
        responses={
            200: OpenApiResponse(
                description="Notification marked as read. Returns the updated notification.",
                response=NotificationSerializer,
            ),
            401: OpenApiResponse(description="Unauthenticated"),
            404: OpenApiResponse(
                description="Notification not found or not owned by the authenticated user"
            ),
        },
    )
    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request: Request, pk: str | None = None) -> Response:
        """POST /notifications/{id}/mark-read/ — mark a notification as read.

        Fetches the notification scoped to the current user (404 if not found or not
        owned), then calls the native mark_read service method. If the notification
        is already READ, returns 200 idempotently without calling mark_read (which
        would raise NotificationUpdateError).

        Returns the serialized updated notification.
        """
        # get_object() uses get_queryset(), which filters by user, notification_type,
        # and status (SENT/READ). This automatically enforces ownership + 404 for
        # non-existent, wrong-user, or wrong-status notifications (e.g., PENDING_SEND).
        notification = self.get_object()

        # Idempotency: if already READ, return 200 without calling mark_read
        # (native method would raise NotificationUpdateError).
        if notification.status == NotificationStatus.READ.value:
            serializer = self.get_serializer(notification)
            return Response(serializer.data, status=status.HTTP_200_OK)

        # Status is SENT — call mark_read to transition to READ.
        # A concurrent request may win the race and mark it READ first; in that
        # case mark_read raises NotificationUpdateError (0-row update).  Treat
        # that as idempotent success — the row is already READ.
        try:
            self.notification_service.mark_read(notification.pk)
        except NotificationUpdateError:
            pass

        # Re-fetch the notification from the DB so created/modified are current
        notification = self.get_object()
        serializer = self.get_serializer(notification)
        return Response(serializer.data, status=status.HTTP_200_OK)

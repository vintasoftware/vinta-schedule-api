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
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from vintasend.services.notification_service import NotificationService

from notifications.serializers import BulkMarkReadSerializer, NotificationSerializer


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
class NotificationViewSet(ViewSet):
    """
    ViewSet for user-scoped in-app notifications.

    All four endpoints read/write exclusively through the native vintasend
    NotificationService — there is NO ORM query or queryset in this view. It is a
    plain DRF ViewSet (not GenericViewSet) precisely because there is no model
    queryset to expose; the serializer is instantiated directly.

    Endpoints:
    - GET /notifications/        — all SENT+READ IN_APP notifications via native
      get_in_app_notifications + get_in_app_notifications_count, page/page_size
      passthrough (envelope: {results, page, page_size, count}).
    - GET /notifications/unread/ — unread (SENT only) via native get_in_app_unread +
      get_in_app_unread_count (same envelope).
    - POST /notifications/{id}/mark-read/ — single mark-read via native
      mark_read_bulk([id], user_id=...) (ownership-scoped, idempotent; 404 on miss).
    - POST /notifications/mark-read-bulk/ — bulk mark-read via native mark_read_bulk.

    Authentication: IsAuthenticated (JWT or session).
    Scope: request.user — no org context.
    """

    permission_classes = (IsAuthenticated,)
    # Schema hint only (drf-spectacular). A plain ViewSet has no serializer machinery;
    # this just tells the OpenAPI generator the default serializer so it doesn't error.
    # Each action's @extend_schema is the source of truth for its request/response shape.
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

    def _paginated_envelope(
        self,
        items: object,
        page: int,
        page_size: int,
        count: int,
    ) -> Response:
        """
        Build the shared passthrough pagination envelope.

        Both list-all and list-unread use this envelope:
        {results: [...], page: int, page_size: int, count: int}
        """
        serializer = NotificationSerializer(items, many=True)
        return Response(
            {
                "results": serializer.data,
                "page": page,
                "page_size": page_size,
                "count": count,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="List all in-app notifications for the authenticated user",
        description=(
            "Returns the authenticated user's IN_APP notifications with status in (SENT, READ). "
            "Ordered by creation date (newest first). "
            "Paginated via page/page_size passthrough. "
            "Envelope: {results: [...], page: int, page_size: int, count: int}."
        ),
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
                description="Number of items per page. Defaults to 10, max 100.",
            ),
        ],
        responses={
            200: OpenApiResponse(
                description=(
                    "Passthrough-paginated list of all in-app notifications. "
                    "Envelope: {results: [...], page: int, page_size: int, count: int}."
                )
            ),
            400: OpenApiResponse(description="Invalid page / page_size parameter"),
            401: OpenApiResponse(description="Unauthenticated"),
        },
    )
    def list(self, request: Request, *args, **kwargs) -> Response:  # type: ignore[override]
        """GET /notifications/ — list all in-app notifications for the current user.

        Returns the authenticated user's IN_APP notifications with status in (SENT, READ),
        ordered by creation date (newest first). Uses native vintasend
        NotificationService.get_in_app_notifications(user_id, page, page_size) for the
        page and get_in_app_notifications_count(user_id) for the total count.
        Passthrough pagination: the caller controls page + page_size.
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

        notifications = list(
            self.notification_service.get_in_app_notifications(user_id, page, page_size)
        )
        count = self.notification_service.get_in_app_notifications_count(user_id)
        return self._paginated_envelope(notifications, page, page_size, count)

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
                    "Envelope: {results: [...], page: int, page_size: int, count: int}. "
                    "count reflects the total number of unread notifications for the user."
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
        (not an ORM queryset). Count comes from get_in_app_unread_count(user_id).
        Passthrough pagination: the caller controls page + page_size.
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
        count = self.notification_service.get_in_app_unread_count(user_id)
        return self._paginated_envelope(notifications, page, page_size, count)

    @extend_schema(
        summary="Mark a notification as read",
        description=(
            "Marks a single in-app notification as read for the authenticated user. "
            "Fully native + ownership-scoped via mark_read_bulk: an id that is missing, "
            "owned by another user, or in a non-SENT/READ state yields 404. Idempotent: "
            "an already-READ owned notification returns 200."
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
        """POST /notifications/{id}/mark-read/ — mark a single notification as read.

        Fully native: delegates to NotificationService.mark_read_bulk([pk],
        user_id=request.user.id), which scopes the update + read-back to the user.
        - foreign / missing / non-SENT-non-READ id → empty result → 404 (IDOR guard);
        - SENT id → transitioned to READ and returned;
        - already-READ owned id → returned unchanged (idempotent 200).
        No ORM query, no get_object() — the native method is the only data path.
        """
        user_id = request.user.id
        if user_id is None:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        results = list(self.notification_service.mark_read_bulk([pk], user_id=user_id))
        if not results:
            raise NotFound("Notification not found.")
        serializer = NotificationSerializer(results[0])
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Mark multiple notifications as read (bulk)",
        description=(
            "Marks multiple in-app notifications as read for the authenticated user. "
            "Ownership-scoped: notifications belonging to other users in the id list are "
            "silently skipped (no IDOR error). Idempotent: already-READ ids are returned "
            "in the results without error. Non-existent ids are silently skipped."
        ),
        request=BulkMarkReadSerializer,
        responses={
            200: OpenApiResponse(
                description=(
                    "Notifications marked as read. "
                    "Envelope: {results: [...serialized notifications that are READ after the op...]}."
                )
            ),
            400: OpenApiResponse(description="Invalid request body (empty or missing ids list)"),
            401: OpenApiResponse(description="Unauthenticated"),
        },
    )
    @action(detail=False, methods=["post"], url_path="mark-read-bulk")
    def mark_read_bulk(self, request: Request) -> Response:
        """POST /notifications/mark-read-bulk/ — mark multiple notifications as read.

        Validates the request body with BulkMarkReadSerializer (non-empty ids list).
        Calls the native NotificationService.mark_read_bulk(ids, user_id=request.user.id)
        which is ownership-scoped (foreign ids silently skipped) and idempotent.
        Returns the serialized notifications that are READ after the operation.
        """
        serializer = BulkMarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = request.user.id
        if user_id is None:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        updated = list(
            self.notification_service.mark_read_bulk(
                serializer.validated_data["ids"],
                user_id=user_id,
            )
        )
        result_serializer = NotificationSerializer(updated, many=True)
        return Response({"results": result_serializer.data}, status=status.HTTP_200_OK)

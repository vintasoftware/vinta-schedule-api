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
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from vintasend.constants import NotificationTypes
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
class NotificationViewSet(GenericViewSet):
    """
    ViewSet for user-scoped in-app notifications.

    Endpoints:
    - GET /notifications/unread/  — list only unread (status=SENT) notifications
      for the authenticated user, using the native vintasend get_in_app_unread.

    Phase 2 will wire up GET /notifications/ (list all) via get_queryset.

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
        ).order_by("-created")

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

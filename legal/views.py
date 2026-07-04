from typing import cast

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from legal.filtersets import PolicyDocumentFilterSet
from legal.models import ConsentSource, PolicyDocument, PolicyDocumentType, UserConsent
from legal.querysets import PolicyDocumentQuerySet
from legal.serializers import (
    ConsentCreateSerializer,
    PolicyDocumentSerializer,
    UserConsentSerializer,
)
from legal.services import ConsentService


@extend_schema(tags=["Legal"])
class PolicyDocumentViewSet(ReadOnlyModelViewSet):
    """Read-only REST surface for policy documents.

    ``PolicyDocument`` is a global, non-tenant-scoped model (privacy policy,
    terms of use, SMS-messaging consent). This viewset intentionally does not
    build on the ``*VintaScheduleModelViewSet`` family: those bases mix in
    ``TenantScopedViewMixin`` (irrelevant — no organization here) and
    ``GenericVirtualModelViewMixin`` (requires a ``VirtualModelSerializer``
    with a ``virtual_model``, which this flat, no-N+1 serializer doesn't need).
    A plain DRF ``ReadOnlyModelViewSet`` mirrors the existing precedent in
    ``organizations.views.ServiceAccountViewSet`` for this shape.

    Auth split (per the plan's Open Questions):
    - ``latest`` / ``latest_by_type`` are **public** (``AllowAny``) — the
      frontend must be able to render policy text before a session exists
      (mid-signup, pre-OAuth-completion).
    - ``list`` (full history) / ``retrieve`` (by id) require authentication —
      these expose the full version history rather than just the
      currently-relevant text, so they stay behind the default auth gate.

    No write surface is exposed anywhere on this viewset.
    """

    queryset = PolicyDocument.objects.all()
    serializer_class = PolicyDocumentSerializer
    permission_classes = (IsAuthenticated,)
    filterset_class = PolicyDocumentFilterSet
    http_method_names = ("get", "head", "options")

    def get_permissions(self):
        if self.action in ("latest", "latest_by_type"):
            return [AllowAny()]
        return super().get_permissions()

    @extend_schema(
        summary="List the latest published version of each policy document type",
        responses={200: PolicyDocumentSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="latest", pagination_class=None)
    def latest(self, request):
        """GET /policy-documents/latest/ — one row per document_type (highest version).

        Public — no authentication required.
        """
        queryset = self.get_queryset().latest_per_type()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Retrieve the latest published version of a single document type",
        parameters=[
            OpenApiParameter(
                name="document_type",
                location=OpenApiParameter.PATH,
                type=str,
                description="One of PolicyDocumentType's values (e.g. sms_consent).",
            ),
        ],
        responses={
            200: PolicyDocumentSerializer,
            404: OpenApiResponse(description="Unknown document_type, or no published version yet"),
        },
    )
    @action(detail=False, methods=["get"], url_path="latest/<str:document_type>")
    def latest_by_type(self, request, document_type: str | None = None):
        """GET /policy-documents/latest/{document_type}/ — highest version of one type.

        Public — no authentication required. 404s on an unknown enum value or a
        type with no published rows yet.
        """
        valid_types = set(PolicyDocumentType.values)
        if document_type not in valid_types:
            raise NotFound(detail=f"Unknown document_type '{document_type}'.")

        queryset = cast(PolicyDocumentQuerySet, self.get_queryset())
        document = queryset.of_type(document_type).order_by("-version").first()
        if document is None:
            raise NotFound(detail=f"No published document of type '{document_type}'.")

        serializer = self.get_serializer(document)
        return Response(serializer.data, status=status.HTTP_200_OK)


def _client_ip_from_request(request) -> str | None:
    """Extract the client IP address from a Django request for audit logging.

    Mirrors ``calendar_integration.mutations._client_ip_from_request``: prefers
    the first entry of ``X-Forwarded-For`` (set by load balancers / proxies);
    falls back to ``REMOTE_ADDR``. Returns ``None`` (rather than ``""``) when
    unavailable, since ``UserConsent.ip_address`` is a nullable
    ``GenericIPAddressField``.
    """
    meta = getattr(request, "META", {})
    forwarded_for = meta.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return meta.get("REMOTE_ADDR") or None


@extend_schema(tags=["Legal"])
class ConsentViewSet(GenericViewSet):
    """Write-only REST surface for recording a :class:`UserConsent` (OAuth step).

    OAuth signups use ``SOCIALACCOUNT_AUTO_SIGNUP`` and collect no phone number
    or consent acknowledgement at signup time. The frontend calls this
    endpoint (authenticated, post-social-login) to record acceptance of a
    published policy document with ``source=ConsentSource.OAUTH_STEP``,
    before requesting phone verification. Mirrors the email-signup capture
    path (``accounts.base_forms.BaseVintaScheduleSignupForm.signup``) for a
    session that already exists.

    No read surface is exposed here — consent history is not a public listing.
    """

    queryset = UserConsent.objects.none()
    serializer_class = ConsentCreateSerializer
    permission_classes = (IsAuthenticated,)
    http_method_names = ("post", "options")

    @extend_schema(
        summary="Record the authenticated user's consent to a policy document type",
        request=ConsentCreateSerializer,
        responses={
            201: UserConsentSerializer,
            400: OpenApiResponse(
                description="Invalid document_type, or no published document of that type yet"
            ),
        },
    )
    def create(self, request, *args, **kwargs):
        """POST /consents/ — record acceptance of `document_type` for the current user.

        Authenticated only. Captures client IP + User-Agent for audit-grade
        proof; delegates version resolution + persistence to `ConsentService`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        consent_service = ConsentService()
        consent = consent_service.record_consent(
            request.user,
            serializer.validated_data["document_type"],
            source=ConsentSource.OAUTH_STEP,
            ip=_client_ip_from_request(request),
            user_agent=request.headers.get("user-agent", ""),
        )

        output_serializer = UserConsentSerializer(consent)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)

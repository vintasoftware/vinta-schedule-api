from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from legal.filtersets import PolicyDocumentFilterSet
from legal.models import PolicyDocument, PolicyDocumentType
from legal.serializers import PolicyDocumentSerializer


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
        queryset = PolicyDocument.objects.latest_per_type()
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

        document = PolicyDocument.objects.latest_for(document_type)
        if document is None:
            raise NotFound(detail=f"No published document of type '{document_type}'.")

        serializer = self.get_serializer(document)
        return Response(serializer.data, status=status.HTTP_200_OK)

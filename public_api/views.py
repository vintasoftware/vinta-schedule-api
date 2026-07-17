import logging

from django.db import transaction
from django.urls import register_converter

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ViewSet

from organizations.models import get_active_organization_membership
from organizations.permissions import IsOrganizationAdmin
from public_api.constants import PROVIDER_SCOPED_RESOURCES
from public_api.docs_content import get_concept_doc, list_concept_docs
from public_api.models import ResourceAccess, SystemUser
from public_api.serializers import (
    ConceptDocSerializer,
    ConceptDocSummarySerializer,
    SystemUserTokenCreateSerializer,
    SystemUserTokenResponseSerializer,
    SystemUserTokenSerializer,
    SystemUserTokenUpdateSerializer,
)


logger = logging.getLogger(__name__)


class SystemUserTokenViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Admin-only viewset for managing public-API tokens (SystemUser + ResourceAccess rows).

    Phase 12: create only.  Phase 13: list + retrieve.
    List / retrieve / revoke / edit-grants are supported phases.

    ``POST /public-api-tokens/`` creates a new ``SystemUser`` for the caller's
    organisation, persists the requested ``ResourceAccess`` rows, and returns the
    plaintext token **once**.  The token is never recoverable after this response.

    ``GET /public-api-tokens/`` lists the caller's org tokens without secrets.
    ``GET /public-api-tokens/{id}/`` retrieves a single token without secrets.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = SystemUserTokenCreateSerializer

    def get_queryset(self):  # type: ignore[override]
        """Org-scoped queryset with prefetched ResourceAccess rows for list/retrieve.

        Prefetches available_resources (related_name on ResourceAccess.system_user FK)
        to avoid N+1 queries when serializing available_resources.
        """
        user = self.request.user
        if not user.is_authenticated:
            return SystemUser.objects.none()
        membership = get_active_organization_membership(user)
        if membership:
            return SystemUser.objects.filter(
                organization_id=membership.organization_id,
                deleted_at__isnull=True,
            ).prefetch_related("available_resources")
        return SystemUser.objects.none()

    def get_serializer_class(self):  # type: ignore[override]
        """Route serializer per action.

        - create: SystemUserTokenCreateSerializer
        - update/partial_update: SystemUserTokenUpdateSerializer
        - list/retrieve: SystemUserTokenSerializer
        """
        if self.action == "create":
            return SystemUserTokenCreateSerializer
        if self.action in ("update", "partial_update"):
            return SystemUserTokenUpdateSerializer
        return SystemUserTokenSerializer

    @extend_schema(
        request=SystemUserTokenCreateSerializer,
        responses={201: SystemUserTokenResponseSerializer},
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Create a SystemUser and ResourceAccess rows; return the plaintext token once.

        Returns HTTP 201 on success.  The response body includes ``id``,
        ``integration_name``, ``is_active``, ``available_resources``, and a
        write-once ``token`` field — never ``long_lived_token_hash``.

        HTTP 400 is returned for:
        - Invalid or unknown ``available_resources`` values.
        - Empty ``available_resources`` list.
        - Duplicate ``integration_name`` (unique constraint).

        HTTP 403 is returned for non-admin callers; HTTP 401 for unauthenticated.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        system_user = serializer.save()

        response_serializer = SystemUserTokenResponseSerializer(
            system_user, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: SystemUserTokenSerializer})
    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request: Request, pk=None) -> Response:
        """Revoke a public-API token by setting its SystemUser.is_active to False.

        The token will no longer authenticate requests via check_system_user_token.
        This is idempotent: revoking an already-revoked token is a 200 no-op.

        Returns HTTP 200 with the updated token serialized via SystemUserTokenSerializer.
        HTTP 403 is returned for non-admin callers; HTTP 404 if the token does not
        exist or belongs to another organization; HTTP 401 for unauthenticated.
        """
        system_user = self.get_object()
        system_user.is_active = False
        system_user.save(update_fields=["is_active"])

        serializer = SystemUserTokenSerializer(system_user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=SystemUserTokenUpdateSerializer,
        responses={200: SystemUserTokenSerializer},
    )
    def update(self, request: Request, *args, **kwargs) -> Response:
        """Update a token's resource grants via PUT (full replacement).

        Accepts ``available_resources`` (a non-empty list of valid resource values).
        Reconciles ResourceAccess rows: adds new, removes dropped, de-duplicates.
        ``integration_name`` and ``token`` are never mutated; if sent in the body,
        they are silently ignored.

        Returns HTTP 200 with the updated token serialized via SystemUserTokenSerializer.
        HTTP 400 is returned for invalid resource values or empty list.
        HTTP 403 is returned for non-admin callers; HTTP 404 if the token does not
        exist or belongs to another organization; HTTP 401 for unauthenticated.
        """
        system_user = self.get_object()

        # Validate input
        input_serializer = SystemUserTokenUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        desired_resources: list[str] = input_serializer.validated_data["available_resources"]

        # Guard: scoped tokens may not be updated with resources outside PROVIDER_SCOPED_RESOURCES.
        if system_user.scoped_to_membership_user_id is not None:
            over_grant = [r for r in desired_resources if r not in PROVIDER_SCOPED_RESOURCES]
            if over_grant:
                raise ValidationError(
                    {
                        "available_resources": [
                            f"Resource(s) not permitted for provider-scoped tokens: "
                            f"{', '.join(over_grant)}. "
                            f"Allowed resources are: {', '.join(sorted(PROVIDER_SCOPED_RESOURCES))}."
                        ]
                    }
                )

        # Reconcile ResourceAccess rows transactionally
        with transaction.atomic():
            # Get currently-granted resources
            current_resources = set(
                ResourceAccess.objects.filter(system_user=system_user).values_list(
                    "resource_name", flat=True
                )
            )
            desired_set = set(desired_resources)

            # Remove dropped resources
            removed_resources = current_resources - desired_set
            if removed_resources:
                ResourceAccess.objects.filter(
                    system_user=system_user, resource_name__in=removed_resources
                ).delete()

            # Add newly-granted resources in a single bulk insert.
            added_resources = desired_set - current_resources
            ResourceAccess.objects.bulk_create(
                [
                    ResourceAccess(system_user=system_user, resource_name=resource_name)
                    for resource_name in added_resources
                ]
            )

        # Refresh and return the updated token
        system_user.refresh_from_db()
        system_user = self.get_queryset().get(pk=system_user.id)  # Re-fetch with prefetch
        serializer = SystemUserTokenSerializer(system_user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=SystemUserTokenUpdateSerializer,
        responses={200: SystemUserTokenSerializer},
    )
    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        """Update a token's resource grants via PATCH (full replacement).

        PATCH and PUT behave identically for this endpoint: both require the full
        ``available_resources`` list and replace grants completely.

        Accepts ``available_resources`` (a non-empty list of valid resource values).
        Reconciles ResourceAccess rows: adds new, removes dropped, de-duplicates.
        ``integration_name`` and ``token`` are never mutated; if sent in the body,
        they are silently ignored.

        Returns HTTP 200 with the updated token serialized via SystemUserTokenSerializer.
        HTTP 400 is returned for invalid resource values or empty list.
        HTTP 403 is returned for non-admin callers; HTTP 404 if the token does not
        exist or belongs to another organization; HTTP 401 for unauthenticated.
        """
        # For this endpoint, PATCH = PUT (full replacement, not merge)
        return self.update(request, *args, **kwargs)


class _ConceptDocSlugConverter:
    """Path converter restricting ``{slug}`` to ``[a-z0-9-]+``.

    ``vinta_schedule_api.urls`` builds ``DefaultRouter(use_regex_path=False)``,
    which generates ``path()``-style routes (``<converter:kwarg>``) rather than
    ``re_path()`` regexes. In that mode DRF's router prefers a viewset's
    ``lookup_value_converter`` over ``lookup_value_regex`` (the latter is only
    consulted when ``use_regex_path=True``). Registering this converter is what
    actually enforces the lowercase-alnum-hyphen charset at the URL-resolution
    layer — anything else (dots, slashes, percent-encoded traversal segments)
    never reaches the view at all.
    """

    regex = "[a-z0-9-]+"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(_ConceptDocSlugConverter, "docs_slug")


@extend_schema(tags=["Docs"])
class PublicApiDocsViewSet(ViewSet):
    """Public, read-only viewset serving the repo's concept documentation.

    ``ConceptDoc`` content is not a model — it is markdown files read from a
    bounded allow-list under ``docs/concepts/`` (see ``public_api.docs_content``).
    This viewset intentionally does not build on the ``*VintaScheduleModelViewSet``
    family, nor even ``ReadOnlyModelViewSet``: those bases assume a queryset and a
    model, and there is neither here — only dicts. Mirrors the shape of
    ``legal.views.PolicyDocumentViewSet`` (public, ``AllowAny``, read-only,
    registered via the same ``RouteDict`` pattern), which is the closest existing
    precedent for a public document-serving endpoint.

    Unauthenticated by design: this is public documentation, not tenant data.
    """

    lookup_field = "slug"
    lookup_value_regex = "[a-z0-9-]+"
    lookup_value_converter = "docs_slug"
    permission_classes = (AllowAny,)
    authentication_classes = ()
    http_method_names = ("get", "head", "options")

    @extend_schema(
        summary="List the available concept docs",
        responses={200: ConceptDocSummarySerializer(many=True)},
    )
    def list(self, request: Request) -> Response:
        """GET /public-api-docs/ — one entry per allow-listed concept doc."""
        serializer = ConceptDocSummarySerializer(list_concept_docs(), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Retrieve a single concept doc's raw markdown",
        responses={
            200: ConceptDocSerializer,
            404: OpenApiResponse(description="Unknown slug (including any traversal attempt)"),
        },
    )
    def retrieve(self, request: Request, slug: str | None = None) -> Response:
        """GET /public-api-docs/{slug}/ — raw markdown for one concept doc.

        ``slug`` is looked up as a key of the allow-list built in
        ``public_api.docs_content``; never joined into a filesystem path. Any
        slug not a key of the allow-list — including path-traversal payloads —
        404s via ``get_concept_doc``.
        """
        doc = get_concept_doc(slug or "")
        serializer = ConceptDocSerializer(doc)
        return Response(serializer.data, status=status.HTTP_200_OK)

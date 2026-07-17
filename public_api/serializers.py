from typing import Annotated

from django.db import IntegrityError, transaction

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from organizations.models import OrganizationMembership, get_active_organization_membership
from public_api.constants import PROVIDER_SCOPED_RESOURCES, PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


class SystemUserTokenCreateSerializer(serializers.Serializer):
    """Input serializer for creating a new public-API token (SystemUser + ResourceAccess rows).

    Accepts ``integration_name``, ``available_resources`` (a non-empty list of valid
    ``PublicAPIResources`` values), and an optional ``scoped_to_user`` (internal User id).
    ``create()`` provisions the ``SystemUser`` via the injected auth service and
    bulk-creates the ``ResourceAccess`` grants, attaching the write-once plaintext
    ``token`` to the returned instance.  Never stores or exposes ``long_lived_token_hash``.

    When ``scoped_to_user`` is supplied and non-null:
    - The referenced user must be an active member of the caller's organisation.
    - ``available_resources`` must be a subset of ``PROVIDER_SCOPED_RESOURCES``.
    When ``scoped_to_user`` is absent or null, behaviour is exactly as before:
    any valid ``PublicAPIResources`` value is accepted and the token is org-wide.
    """

    integration_name = serializers.CharField(max_length=150, required=True)
    available_resources = serializers.ListField(
        child=serializers.ChoiceField(choices=PublicAPIResources.choices),
        required=True,
        allow_empty=False,
    )
    scoped_to_user = serializers.IntegerField(required=False, allow_null=True)

    @inject
    def __init__(
        self,
        *args,
        public_api_auth_service: Annotated[
            "PublicAPIAuthService | None", Provide["public_api_auth_service"]
        ] = None,
        **kwargs,
    ) -> None:
        self.public_api_auth_service = public_api_auth_service
        super().__init__(*args, **kwargs)

    def validate(self, attrs: dict) -> dict:
        """Cross-field validation: when scoped_to_user is present and non-null, resolve the
        active OrganizationMembership of that user in the caller's org and enforce the provider
        allow-list on available_resources.  When scoped_to_user is absent or null, no
        additional constraints are applied — the no-owner path is byte-for-byte identical
        to the pre-Phase-3 behaviour.

        The input field ``scoped_to_user`` is a User id (external REST API contract).  Internally
        the membership FK is resolved and stashed as ``_resolved_membership``; only the membership
        is ever stored — the User id is derived from it on read.
        """
        scoped_to_user_id: int | None = attrs.get("scoped_to_user")
        if scoped_to_user_id is None:
            # No owner — org-wide token; no additional validation needed.
            return attrs

        request = self.context["request"]
        caller_membership: OrganizationMembership | None = get_active_organization_membership(
            request.user
        )
        if caller_membership is None:
            raise PermissionDenied("No active organisation membership.")

        # Resolve the owner: the target user must be an active member of the caller's org.
        # This single query both validates active membership AND yields the value to store.
        try:
            resolved_membership = OrganizationMembership.objects.get(
                user_id=scoped_to_user_id,
                organization=caller_membership.organization,
                is_active=True,
            )
        except OrganizationMembership.DoesNotExist as e:
            raise serializers.ValidationError(
                {
                    "scoped_to_user": [
                        f"User with id '{scoped_to_user_id}' is not an active member of "
                        "the caller's organization."
                    ]
                }
            ) from e

        # Enforce provider allow-list: every requested resource must be in PROVIDER_SCOPED_RESOURCES.
        available_resources: list[str] = attrs["available_resources"]
        over_grant = [r for r in available_resources if r not in PROVIDER_SCOPED_RESOURCES]
        if over_grant:
            raise serializers.ValidationError(
                {
                    "available_resources": [
                        f"Resource(s) not permitted for provider-scoped tokens: "
                        f"{', '.join(over_grant)}. "
                        f"Allowed resources are: {', '.join(sorted(PROVIDER_SCOPED_RESOURCES))}."
                    ]
                }
            )

        # Stash the resolved membership so create() can pass it to create_system_user.
        attrs["_resolved_membership"] = resolved_membership
        return attrs

    def create(self, validated_data: dict) -> SystemUser:
        request = self.context["request"]
        membership = get_active_organization_membership(request.user)
        if membership is None:
            # IsOrganizationAdmin already guards this; defensive fallback.
            raise PermissionDenied("No active organisation membership.")

        integration_name: str = validated_data["integration_name"]
        available_resources: list[str] = validated_data["available_resources"]
        # Pop the internal stash set by validate(); None when no owner was supplied.
        resolved_membership = validated_data.pop("_resolved_membership", None)

        try:
            with transaction.atomic():
                assert self.public_api_auth_service is not None  # noqa: S101 — injected at construction
                system_user, plaintext_token = self.public_api_auth_service.create_system_user(
                    integration_name=integration_name,
                    organization=membership.organization,
                    scoped_to_membership=resolved_membership,
                )
                # dict.fromkeys dedupes while preserving order, so the unique
                # (system_user, resource_name) constraint cannot be violated.
                ResourceAccess.objects.bulk_create(
                    [
                        ResourceAccess(system_user=system_user, resource_name=resource_name)
                        for resource_name in dict.fromkeys(available_resources)
                    ]
                )
        except IntegrityError as e:
            raise serializers.ValidationError(
                {
                    "integration_name": [
                        f"A token with integration_name '{integration_name}' already exists."
                    ]
                }
            ) from e

        # Expose the plaintext token once via a pseudo-attribute for the response serializer.
        system_user.token = plaintext_token  # type: ignore[attr-defined]
        return system_user


class SystemUserTokenResponseSerializer(serializers.ModelSerializer):
    """Read serializer for the created SystemUser.

    Includes the write-once ``token`` field (sourced from the view) and
    ``available_resources`` (derived from the related ``ResourceAccess`` rows).
    Exposes ``scoped_to_user`` as the owner's User id derived from the stored membership
    reference (null for org-wide tokens).  The REST field name ``scoped_to_user`` is kept
    for API stability; the value is the denormalized ``scoped_to_membership_user_id``.
    Never exposes ``long_lived_token_hash``.
    """

    available_resources = serializers.SerializerMethodField()
    token = serializers.CharField(read_only=True)
    scoped_to_user = serializers.SerializerMethodField()

    class Meta:
        model = SystemUser
        fields = (
            "id",
            "integration_name",
            "is_active",
            "available_resources",
            "scoped_to_user",
            "token",
        )
        read_only_fields = fields

    def get_available_resources(self, obj: SystemUser) -> list[str]:
        """Return a list of resource_name values from the related ResourceAccess rows."""
        return list(
            ResourceAccess.objects.filter(system_user=obj).values_list("resource_name", flat=True)
        )

    def get_scoped_to_user(self, obj: SystemUser) -> int | None:
        """Return the owner's User id from the denormalized membership column, or None.

        ``scoped_to_membership_user_id`` already stores the membership's user_id, so the
        value is returned directly with no extra query.
        """
        return obj.scoped_to_membership_user_id


class SystemUserTokenSerializer(serializers.ModelSerializer):
    """Read-only serializer for listing and retrieving public-API tokens.

    Exposes ``id``, ``integration_name``, ``is_active``, ``available_resources``
    (list of resource_name strings from the related ``ResourceAccess`` rows), and
    ``scoped_to_user`` (the owner's User id from the denormalized membership column, null
    for org-wide tokens).  The REST field name ``scoped_to_user`` is kept for API stability;
    the value is the denormalized ``scoped_to_membership_user_id``.
    Never exposes ``long_lived_token_hash`` or ``token``.

    Optimized for list queries: uses prefetched ``available_resources`` from the viewset's
    ``get_queryset`` to avoid N+1 queries.  ``scoped_to_membership_user_id`` is a concrete
    column on the row, so it needs no join.
    """

    available_resources = serializers.SerializerMethodField()
    scoped_to_user = serializers.SerializerMethodField()

    class Meta:
        model = SystemUser
        fields = ("id", "integration_name", "is_active", "available_resources", "scoped_to_user")
        read_only_fields = fields

    def get_available_resources(self, obj: SystemUser) -> list[str]:
        """Return a list of resource_name values from the prefetched ResourceAccess rows."""
        return [ra.resource_name for ra in obj.available_resources.all()]

    def get_scoped_to_user(self, obj: SystemUser) -> int | None:
        """Return the owner's User id from the denormalized membership column, or None.

        ``scoped_to_membership_user_id`` is a concrete column already storing the
        membership's user_id, so the value is returned directly with no extra query.
        """
        return obj.scoped_to_membership_user_id


class ConceptDocSummarySerializer(serializers.Serializer):
    """Read-only manifest entry for a concept doc (list view).

    Plain ``Serializer`` over a :class:`public_api.docs_content.ConceptDocSummary`
    dict — there is no model backing this.
    """

    slug = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)


class ConceptDocSerializer(ConceptDocSummarySerializer):
    """Read-only representation of a single concept doc's full content.

    Plain ``Serializer`` over a :class:`public_api.docs_content.ConceptDoc` dict —
    there is no model backing this. Returns raw markdown; the frontend owns
    rendering and sanitization.
    """

    markdown = serializers.CharField(read_only=True)


class SystemUserTokenUpdateSerializer(serializers.Serializer):
    """Input serializer for updating a public-API token's resource grants (Phase 15).

    Accepts ``available_resources`` (a non-empty list of valid ``PublicAPIResources`` values)
    only.  ``integration_name`` and ``token`` are never accepted or changed.
    The view reconciles ResourceAccess rows: adds rows for newly-granted resources,
    removes rows for dropped resources.
    """

    available_resources = serializers.ListField(
        child=serializers.ChoiceField(choices=PublicAPIResources.choices),
        required=True,
        allow_empty=False,
    )

from typing import Annotated

from django.db import IntegrityError, transaction

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from organizations.models import get_active_organization_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


class SystemUserTokenCreateSerializer(serializers.Serializer):
    """Input serializer for creating a new public-API token (SystemUser + ResourceAccess rows).

    Accepts ``integration_name`` and ``available_resources`` (a non-empty list of valid
    ``PublicAPIResources`` values).  ``create()`` provisions the ``SystemUser`` via the
    injected auth service and bulk-creates the ``ResourceAccess`` grants, attaching the
    write-once plaintext ``token`` to the returned instance.  Never stores or exposes
    ``long_lived_token_hash``.
    """

    integration_name = serializers.CharField(max_length=150, required=True)
    available_resources = serializers.ListField(
        child=serializers.ChoiceField(choices=PublicAPIResources.choices),
        required=True,
        allow_empty=False,
    )

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

    def create(self, validated_data: dict) -> SystemUser:
        request = self.context["request"]
        membership = get_active_organization_membership(request.user)
        if membership is None:
            # IsOrganizationAdmin already guards this; defensive fallback.
            raise PermissionDenied("No active organisation membership.")

        integration_name: str = validated_data["integration_name"]
        available_resources: list[str] = validated_data["available_resources"]

        try:
            with transaction.atomic():
                system_user, plaintext_token = self.public_api_auth_service.create_system_user(
                    integration_name=integration_name,
                    organization=membership.organization,
                )
                ResourceAccess.objects.bulk_create(
                    [
                        ResourceAccess(system_user=system_user, resource_name=resource_name)
                        for resource_name in available_resources
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
    Never exposes ``long_lived_token_hash``.
    """

    available_resources = serializers.SerializerMethodField()
    token = serializers.CharField(read_only=True)

    class Meta:
        model = SystemUser
        fields = ("id", "integration_name", "is_active", "available_resources", "token")
        read_only_fields = fields

    def get_available_resources(self, obj: SystemUser) -> list[str]:
        """Return a list of resource_name values from the related ResourceAccess rows."""
        return list(
            ResourceAccess.objects.filter(system_user=obj).values_list("resource_name", flat=True)
        )


class SystemUserTokenSerializer(serializers.ModelSerializer):
    """Read-only serializer for listing and retrieving public-API tokens.

    Exposes ``id``, ``integration_name``, ``is_active``, and ``available_resources``
    (list of resource_name strings from the related ``ResourceAccess`` rows).
    Never exposes ``long_lived_token_hash`` or ``token``.

    Optimized for list queries: uses prefetched ``available_resources`` from
    the viewset's ``get_queryset`` to avoid N+1 queries.
    """

    available_resources = serializers.SerializerMethodField()

    class Meta:
        model = SystemUser
        fields = ("id", "integration_name", "is_active", "available_resources")
        read_only_fields = fields

    def get_available_resources(self, obj: SystemUser) -> list[str]:
        """Return a list of resource_name values from the prefetched ResourceAccess rows."""
        return [ra.resource_name for ra in obj.available_resources.all()]


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

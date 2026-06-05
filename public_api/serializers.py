from rest_framework import serializers

from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser


class SystemUserTokenCreateSerializer(serializers.Serializer):
    """Input serializer for creating a new public-API token (SystemUser + ResourceAccess rows).

    Accepts ``integration_name`` and ``available_resources`` (a non-empty list of valid
    ``PublicAPIResources`` values).  On a successful create the view adds a write-once
    ``token`` field to the response data; this serializer never stores or exposes
    ``long_lived_token_hash``.
    """

    integration_name = serializers.CharField(max_length=150, required=True)
    available_resources = serializers.ListField(
        child=serializers.ChoiceField(choices=PublicAPIResources.choices),
        required=True,
        allow_empty=False,
    )


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

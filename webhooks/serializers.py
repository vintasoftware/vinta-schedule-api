from typing import Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from common.utils.serializer_utils import VirtualModelSerializer
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services import WebhookService
from webhooks.virtual_models import WebhookConfigurationVirtualModel, WebhookEventVirtualModel


class WebhookConfigurationSerializer(VirtualModelSerializer):
    class Meta:
        model = WebhookConfiguration
        virtual_model = WebhookConfigurationVirtualModel
        fields = (
            "id",
            "event_type",
            "url",
            "headers",
        )

    @inject
    def __init__(
        self,
        *args,
        webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.webhook_service = webhook_service

    def create(self, validated_data):
        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )
        return self.webhook_service.create_configuration(
            organization=user.organization_membership.organization,
            event_type=validated_data["event_type"],
            url=validated_data["url"],
            headers=validated_data.get("headers", {}),
        )

    def update(self, instance, validated_data):
        return self.webhook_service.update_configuration(
            configuration=instance,
            event_type=validated_data.get("event_type", instance.event_type),
            url=validated_data.get("url", instance.url),
            headers=validated_data.get("headers", instance.headers),
        )


class WebhookEventSerializer(VirtualModelSerializer):
    configuration: serializers.PrimaryKeyRelatedField = serializers.PrimaryKeyRelatedField(
        source="configuration_fk",
        read_only=True,
    )
    main_event: serializers.PrimaryKeyRelatedField = serializers.PrimaryKeyRelatedField(
        source="main_event_fk",
        read_only=True,
    )

    class Meta:
        model = WebhookEvent
        virtual_model = WebhookEventVirtualModel
        fields = (
            "configuration",
            "main_event",
            "event_type",
            "url",
            "status",
            "headers",
            "payload",
            "response_status",
            "response_body",
            "response_headers",
            "retry_number",
            "send_after",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "status",
            "response_status",
            "response_body",
            "response_headers",
            "retry_number",
            "send_after",
            "created",
            "modified",
            "created",
            "modified",
        )

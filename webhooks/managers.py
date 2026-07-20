from organizations.managers import BaseOrganizationModelManager
from webhooks.querysets import WebhookConfigurationQuerySet


class WebhookConfigurationManager(BaseOrganizationModelManager):
    """Manager for WebhookConfiguration with domain-specific query methods."""

    def get_queryset(self) -> WebhookConfigurationQuerySet:
        return WebhookConfigurationQuerySet(self.model, using=self._db)

    def filter_by_organization(self, organization_id: int) -> WebhookConfigurationQuerySet:
        return self.get_queryset().filter(organization_id=organization_id)  # type: ignore[return-value]

    def live(self) -> WebhookConfigurationQuerySet:
        """Wraps :meth:`WebhookConfigurationQuerySet.live`."""
        return self.get_queryset().live()

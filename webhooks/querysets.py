from common.querysets import OrganizationScopedQuerySet


class WebhookConfigurationQuerySet(OrganizationScopedQuerySet):
    """QuerySet for WebhookConfiguration with domain-specific filtering methods."""

    def live(self) -> "WebhookConfigurationQuerySet":
        """Return only non-soft-deleted configurations."""
        return self.filter(deleted_at__isnull=True)

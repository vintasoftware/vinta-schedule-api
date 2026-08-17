from common.managers import OrganizationScopedManager
from webhooks.querysets import WebhookConfigurationQuerySet


# ``from_queryset`` rather than a hand-rolled ``get_queryset`` returning
# ``WebhookConfigurationQuerySet(self.model, using=self._db)``. Building the
# queryset directly skips ``OrganizationScopedManager.get_queryset`` entirely, so
# ``objects`` would *look* scoped while reading every tenant -- the defect Phase 2a
# found in all 12 ``calendar_integration`` managers. Going through
# ``from_queryset`` also keeps ``_queryset_class`` and the copied queryset methods
# pointed at the same class.
_WebhookConfigurationManagerBase = OrganizationScopedManager.from_queryset(
    WebhookConfigurationQuerySet
)


class WebhookConfigurationManager(_WebhookConfigurationManagerBase):  # type: ignore[misc,valid-type]
    """Manager for WebhookConfiguration with domain-specific query methods.

    ``live()`` is copied off :class:`~webhooks.querysets.WebhookConfigurationQuerySet`
    by ``from_queryset``; ``filter_by_organization`` / ``exclude_by_organization`` /
    ``unscoped`` come from :class:`~common.managers.OrganizationScopedManager`.

    The old hand-written ``filter_by_organization(organization_id)`` is gone:
    the inherited one takes an ``Organization`` *or* its id and starts from the
    unscoped queryset, which is what makes a deliberate cross-organization read
    expressible under ``STRICT_ORGANIZATION_FILTER``.
    """

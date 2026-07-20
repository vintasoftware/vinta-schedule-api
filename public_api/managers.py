from organizations.managers import BaseOrganizationModelManager
from public_api.querysets import SystemUserQuerySet


class SystemUserManager(BaseOrganizationModelManager):
    """Manager for SystemUser with domain-specific query methods."""

    def get_queryset(self) -> SystemUserQuerySet:
        return SystemUserQuerySet(self.model, using=self._db)

    def live(self) -> SystemUserQuerySet:
        """Wraps :meth:`SystemUserQuerySet.live`."""
        return self.get_queryset().live()

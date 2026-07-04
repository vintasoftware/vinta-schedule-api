from typing import TYPE_CHECKING

from django.db import models

from legal.querysets import PolicyDocumentQuerySet


if TYPE_CHECKING:
    from legal.models import PolicyDocument


class PolicyDocumentManager(models.Manager):
    """Manager for PolicyDocument exposing latest-version lookups.

    ``PolicyDocument`` is a global (non-tenant-scoped) model, so this manager
    does not enforce an organization filter — unlike `OrganizationManager`.
    """

    def get_queryset(self) -> PolicyDocumentQuerySet:
        return PolicyDocumentQuerySet(self.model, using=self._db)

    def of_type(self, document_type: str) -> PolicyDocumentQuerySet:
        """Return all versions of a single document_type."""
        return self.get_queryset().of_type(document_type)

    def latest_for(self, document_type: str) -> "PolicyDocument | None":
        """Return the highest-version published row for `document_type`, or None."""
        return self.of_type(document_type).order_by("-version").first()

    def latest_per_type(self) -> PolicyDocumentQuerySet:
        """Return one row per document_type: the highest-version row."""
        return self.get_queryset().latest_per_type()

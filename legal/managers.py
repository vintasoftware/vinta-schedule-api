from typing import TYPE_CHECKING

from django.db import models

from legal.querysets import PolicyDocumentQuerySet, UserConsentQuerySet


if TYPE_CHECKING:
    from legal.models import PolicyDocument
    from users.models import User


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


class UserConsentManager(models.Manager):
    """Manager for UserConsent exposing the consent-gate predicate.

    ``UserConsent`` is a global (non-tenant-scoped) model, so this manager
    does not enforce an organization filter — unlike `OrganizationManager`.
    """

    def get_queryset(self) -> UserConsentQuerySet:
        return UserConsentQuerySet(self.model, using=self._db)

    def has_sms_consent(self, user: "User") -> bool:
        """Return True if `user` has ever accepted any version of SMS_CONSENT.

        Re-consent policy is consent-once-ever: any prior SMS_CONSENT row
        (regardless of version) satisfies the gate forever.
        """
        # Local import: legal.models imports legal.managers at module load, so
        # importing PolicyDocumentType at module top here would be circular.
        from legal.models import PolicyDocumentType

        return (
            self.get_queryset()
            .for_user(user)
            .for_document_type(PolicyDocumentType.SMS_CONSENT)
            .exists()
        )

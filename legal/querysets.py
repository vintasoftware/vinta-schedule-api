from typing import TYPE_CHECKING

from django.db import models

from common.utils.phone_utils import normalize_phone_number


if TYPE_CHECKING:
    from users.models import User


class PolicyDocumentQuerySet(models.QuerySet):
    """Chainable queryset for PolicyDocument."""

    def of_type(self, document_type: str) -> "PolicyDocumentQuerySet":
        """Filter to rows of a single document_type."""
        return self.filter(document_type=document_type)

    def latest_per_type(self) -> "PolicyDocumentQuerySet":
        """Return one row per document_type: the highest-version row.

        Implemented with Postgres ``DISTINCT ON`` via ``QuerySet.distinct(*fields)``
        (Postgres-only API). The project's sole supported database is Postgres, so
        this avoids a less efficient group-by-then-fetch round trip.
        """
        return self.order_by("document_type", "-version").distinct("document_type")


class UserConsentQuerySet(models.QuerySet):
    """Chainable queryset for UserConsent."""

    def for_document_type(self, document_type: str) -> "UserConsentQuerySet":
        """Filter to consent rows accepting any version of `document_type`."""
        return self.filter(policy_document__document_type=document_type)

    def for_user(self, user: "User") -> "UserConsentQuerySet":
        """Filter to consent rows belonging to `user`."""
        return self.filter(user=user)

    def for_phone(self, phone: str) -> "UserConsentQuerySet":
        """Filter to consent rows recorded against `phone`.

        `phone` is normalized (see `common.utils.phone_utils.normalize_phone_number`)
        before matching, so a `phone_number` stored via a client that posted a
        human-formatted number (e.g. ``"+1 555-555-0100"``) still compares equal
        to the E.164 value allauth passes at verification time.

        Never matches a blank `phone` — a blank `phone` argument (or a blank
        stored value) must not satisfy a phone-keyed consent check. Does NOT
        by itself enforce phone ownership — see
        `UserConsentManager.has_sms_consent_for_phone` (join-filters on
        `user__phone_number=phone` on top of this) and
        `UserConsentManager.has_sms_consent_for_phone_and_user` (chains
        `for_user` on top of this instead).
        """
        phone = normalize_phone_number(phone)
        if not phone:
            return self.none()
        return self.filter(phone_number=phone)

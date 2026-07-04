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

    def has_sms_consent_for_phone(self, phone: str) -> bool:
        """Return True if an SMS_CONSENT row for `phone`, owned by `phone`, exists.

        Phone-keyed, `user`-independent — this is the gate used for the two
        anti-enumeration SMS sends (`send_unknown_account_sms` /
        `send_account_already_exists_sms`), which have no `user` at their call
        site, only a submitted phone. A consent row only satisfies the gate
        when its own `user.phone_number` also equals `phone`
        (`user__phone_number=phone`) — this stops an attacker fabricating a
        consent row for a phone they don't own (their own user, a victim's
        phone) from unlocking these sends for that phone. A blank `phone`
        never matches. See `has_sms_consent_for_phone_and_user` for the
        user-tied variant used by `send_verification_code_sms`.
        """
        # Local import: legal.models imports legal.managers at module load, so
        # importing PolicyDocumentType at module top here would be circular.
        from legal.models import PolicyDocumentType

        return (
            self.get_queryset()
            .for_phone(phone)
            .filter(user__phone_number=phone)
            .for_document_type(PolicyDocumentType.SMS_CONSENT)
            .exists()
        )

    def has_sms_consent_for_phone_and_user(self, phone: str, user: "User") -> bool:
        """Return True if `user` recorded an SMS_CONSENT row for `phone`.

        Phone-keyed AND tied to the specific `user` requesting verification —
        the gate used by `send_verification_code_sms`, which always carries a
        `user`. Requiring `user.phone_number == phone` instead (as
        `has_sms_consent_for_phone` does for the anti-enumeration sends) is
        not viable here: for a genuinely new phone submission, allauth's
        `ChangePhoneForm.clean_phone` rejects the request outright when the
        submitted phone equals the user's *current* phone
        (`same_as_current`), and `set_phone` only runs *after* the code is
        verified (`PhoneVerificationProcess.finish`) — so at gate-check time
        `user.phone_number` can never equal the phone being verified for a
        first-time add/change. Tying the gate to "this user recorded consent
        for this phone" instead still closes the fabrication vector (a
        consent row recorded by a different user never matches) without
        depending on that ordering.
        """
        # Local import: legal.models imports legal.managers at module load, so
        # importing PolicyDocumentType at module top here would be circular.
        from legal.models import PolicyDocumentType

        return (
            self.get_queryset()
            .for_phone(phone)
            .for_user(user)
            .for_document_type(PolicyDocumentType.SMS_CONSENT)
            .exists()
        )

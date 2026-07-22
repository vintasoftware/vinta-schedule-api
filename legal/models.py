from typing import ClassVar

from django.db import models
from django.utils import timezone

from common.models import BaseModel
from legal.managers import PolicyDocumentManager, UserConsentManager


class PolicyDocumentType(models.TextChoices):
    PRIVACY_POLICY = "privacy_policy", "Privacy Policy"
    TERMS_OF_USE = "terms_of_use", "Terms of Use"
    SMS_CONSENT = "sms_consent", "SMS Messaging Consent"


class PolicyDocument(BaseModel):
    """An immutable, versioned policy document.

    Covers privacy policy, terms of use, and SMS-messaging consent text, stored
    as raw markdown (rendered client-side; no markdown library in the repo).
    Each publish creates a new row — existing rows are never edited after
    publish (enforced in `legal/admin.py`, not just convention). "Latest" is
    the highest `version` for a given `document_type` among published rows.

    Global — not tenant-scoped. `users.User` is a global model and policy
    documents are Vinta-owned (not per-organization) in v1, so this is a plain
    `BaseModel`, not an `OrganizationModel`; it carries no `organization` FK.
    """

    document_type = models.CharField(max_length=32, choices=PolicyDocumentType)
    version = models.PositiveIntegerField(help_text="Monotonically increasing per document_type.")
    title = models.CharField(max_length=255)
    body_markdown = models.TextField(help_text="Raw markdown body, rendered client-side.")
    published_at = models.DateTimeField(default=timezone.now)

    objects: PolicyDocumentManager = PolicyDocumentManager()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["document_type", "version"],
                name="uq_policydocument_type_version",
            ),
        ]
        indexes: ClassVar = [models.Index(fields=["document_type", "-version"])]
        ordering: ClassVar = ["document_type", "-version"]

    def __str__(self) -> str:
        return f"{self.get_document_type_display()} v{self.version}"


class ConsentSource(models.TextChoices):
    SIGNUP_FORM = "signup_form", "Signup Form"
    OAUTH_STEP = "oauth_step", "OAuth Consent Step"
    API = "api", "API"


class UserConsent(BaseModel):
    """An append-only, audit-grade record of a user accepting a policy version.

    One row is created every time a user accepts a specific `PolicyDocument`
    version — existing rows are never edited (append-only). The exact version
    accepted is pinned via `policy_document`, and `accepted_at` / `ip_address`
    / `user_agent` / `source` provide audit-grade proof of acceptance (e.g. for
    Twilio/carrier SMS opt-in disputes).

    Global — not tenant-scoped. `users.User` is a global model and consent is
    per-user, not per-organization, so this is a plain `BaseModel`, not an
    `OrganizationModel`; it carries no `organization` FK.

    Re-consent policy (consent-once-ever): the SMS-consent gate is satisfied by
    ANY `UserConsent` row whose document is of type `SMS_CONSENT`, regardless
    of version — see `UserConsentManager.has_sms_consent`.

    Phone-keyed consent: `phone_number` records the phone submitted
    at consent time (signup, or the OAuth-step `/consents/` endpoint),
    normalized via `common.utils.phone_utils.normalize_phone_number`. `user`
    stays required — every recording site has one — and the SMS gate ties
    ownership to that `user`: the anti-enumeration sends (no `user` at their
    call site) require the row's own `user.phone_number` to also equal
    `phone_number` (`UserConsentManager.has_sms_consent_for_phone`); the
    verification-code send (which always has a `user`) requires the row to
    belong to that same `user` (`UserConsentManager.has_sms_consent_for_phone_and_user`).
    Either way, a row recorded by one user for another's phone number cannot
    unlock SMS to that phone.
    """

    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="consents")
    # PROTECT: proof integrity. An accepted policy version must never be
    # deletable while a UserConsent row still references it as what the user saw.
    policy_document = models.ForeignKey(
        PolicyDocument, on_delete=models.PROTECT, related_name="consents"
    )
    accepted_at = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    source = models.CharField(max_length=16, choices=ConsentSource)
    phone_number = models.CharField(max_length=20, blank=True, default="")

    objects: UserConsentManager = UserConsentManager()

    class Meta:
        indexes: ClassVar = [
            models.Index(fields=["user", "policy_document"]),
            models.Index(fields=["phone_number"], name="legal_userconsent_phone_idx"),
        ]

    def __str__(self) -> str:
        return f"Consent by user {self.user_id} for {self.policy_document}"

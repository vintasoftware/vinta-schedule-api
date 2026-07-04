from typing import ClassVar

from django.db import models
from django.utils import timezone

from common.models import BaseModel
from legal.managers import PolicyDocumentManager


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

from model_bakery import baker

from legal.models import PolicyDocument, PolicyDocumentType


class PolicyDocumentFactory:
    """Factory for creating PolicyDocument instances in tests.

    `PolicyDocument` is a global model (not tenant-scoped — no `organization`
    FK), so unlike tenant-scoped factories this does not require an
    `organization` argument.
    """

    def create(
        self,
        document_type: str = PolicyDocumentType.PRIVACY_POLICY,
        **overrides,
    ) -> PolicyDocument:
        """Create and persist a PolicyDocument, auto-incrementing `version`.

        Provides sensible defaults for required fields while allowing any
        field to be overridden via keyword arguments. If `version` isn't
        passed explicitly, it defaults to one more than the current highest
        version for `document_type` (starting at 1).

        Args:
            document_type: One of `PolicyDocumentType`'s values.
            **overrides: Any PolicyDocument field values to override the defaults.

        Returns:
            A persisted PolicyDocument instance.
        """
        defaults: dict = {
            "document_type": document_type,
            "title": "Policy Document",
            "body_markdown": "# Policy\n\nBody text.",
        }
        defaults.update(overrides)
        if "version" not in overrides:
            latest = PolicyDocument.objects.latest_for(document_type)
            defaults["version"] = (latest.version + 1) if latest is not None else 1
        return baker.make(PolicyDocument, **defaults)

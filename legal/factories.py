from model_bakery import baker

from legal.models import ConsentSource, PolicyDocument, PolicyDocumentType, UserConsent


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


class UserConsentFactory:
    """Factory for creating UserConsent instances in tests.

    `UserConsent` is a global model (not tenant-scoped — no `organization`
    FK). Requires a `user`; creates a `PolicyDocument` of `document_type` via
    `PolicyDocumentFactory` when no `policy_document` override is passed.
    """

    def create(
        self,
        user,
        document_type: str = PolicyDocumentType.SMS_CONSENT,
        **overrides,
    ) -> UserConsent:
        """Create and persist a UserConsent, defaulting sensible required fields.

        Args:
            user: The consenting user.
            document_type: Used to resolve/create the `PolicyDocument` when
                `policy_document` is not explicitly overridden.
            **overrides: Any UserConsent field values to override the defaults.

        Returns:
            A persisted UserConsent instance.
        """
        defaults: dict = {
            "user": user,
            "source": ConsentSource.SIGNUP_FORM,
        }
        if "policy_document" not in overrides:
            defaults["policy_document"] = PolicyDocumentFactory().create(
                document_type=document_type
            )
        defaults.update(overrides)
        return baker.make(UserConsent, **defaults)

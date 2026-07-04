from rest_framework import serializers

from legal.models import PolicyDocument, PolicyDocumentType, UserConsent


class PolicyDocumentSerializer(serializers.ModelSerializer):
    """Read-only representation of a published :class:`PolicyDocument` version.

    Every field is read-only — this app exposes no write surface for policy
    documents over the REST API; documents are authored in Django admin.
    """

    class Meta:
        model = PolicyDocument
        fields = ("id", "document_type", "version", "title", "body_markdown", "published_at")
        read_only_fields = fields


class UserConsentSerializer(serializers.ModelSerializer):
    """Read-only representation of a recorded :class:`UserConsent`.

    Returned by ``ConsentViewSet.create`` after ``ConsentService.record_consent``
    persists the acceptance; every field is read-only here too.
    """

    document_type = serializers.CharField(source="policy_document.document_type", read_only=True)
    policy_document_version = serializers.IntegerField(
        source="policy_document.version", read_only=True
    )

    class Meta:
        model = UserConsent
        fields = (
            "id",
            "document_type",
            "policy_document",
            "policy_document_version",
            "source",
            "accepted_at",
            "ip_address",
            "user_agent",
            "phone_number",
        )
        read_only_fields = fields


class ConsentCreateSerializer(serializers.Serializer):
    """Validates the input for the authenticated consent-record endpoint (OAuth step).

    ``document_type`` is required — the consenting user comes from the
    authenticated request, and audit metadata (IP, user-agent, source) is
    captured server-side in the view. ``phone_number`` is optional (Phase 8 —
    phone-keyed consent): an OAuth user can consent a phone number here,
    before phone verification, so the SMS gate can later be satisfied for
    that phone.
    """

    document_type = serializers.ChoiceField(choices=PolicyDocumentType.choices)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

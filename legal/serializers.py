from rest_framework import serializers

from legal.models import PolicyDocument


class PolicyDocumentSerializer(serializers.ModelSerializer):
    """Read-only representation of a published :class:`PolicyDocument` version.

    Every field is read-only — this app exposes no write surface for policy
    documents over the REST API; documents are authored in Django admin.
    """

    class Meta:
        model = PolicyDocument
        fields = ("id", "document_type", "version", "title", "body_markdown", "published_at")
        read_only_fields = fields

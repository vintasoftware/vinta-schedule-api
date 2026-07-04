from django import forms
from django.contrib import admin
from django.http import HttpRequest

from legal.models import PolicyDocument, PolicyDocumentType


class PolicyDocumentAdminForm(forms.ModelForm):
    """ModelForm that auto-suggests the next `version` per document type.

    On the add form, `version`'s help text is replaced with the next
    available version for every `document_type` (`max(version) + 1`, or `1`
    if none published yet), computed server-side. This is a suggestion
    only — the `(document_type, version)` unique constraint is the actual
    guard against duplicate versions.
    """

    class Meta:
        model = PolicyDocument
        fields = (
            "document_type",
            "version",
            "title",
            "body_markdown",
            "published_at",
            "meta",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Existing (published) rows are read-only after creation — no
            # suggestion needed on the change form.
            return
        suggestions = ", ".join(
            f"{label} ({document_type}): {self._next_version(document_type)}"
            for document_type, label in PolicyDocumentType.choices
        )
        self.fields["version"].help_text = f"Suggested next version per type — {suggestions}."

    @staticmethod
    def _next_version(document_type: str) -> int:
        latest = PolicyDocument.objects.latest_for(document_type)
        return (latest.version + 1) if latest is not None else 1


@admin.register(PolicyDocument)
class PolicyDocumentAdmin(admin.ModelAdmin):
    """Admin for PolicyDocument.

    Published rows are immutable: once a row exists, every field is
    read-only on the change form (enforced via `get_readonly_fields`, not
    just convention) so an admin cannot rewrite the text a user may have
    already seen/accepted. New versions are authored by adding a new row.
    """

    form = PolicyDocumentAdminForm
    list_display = ("title", "document_type", "version", "published_at")
    list_filter = ("document_type",)
    search_fields = ("title",)
    ordering = ("-version",)

    def get_readonly_fields(
        self, request: HttpRequest, obj: PolicyDocument | None = None
    ) -> list[str]:
        """Lock every field once the row has been published (i.e. it has a pk)."""
        if obj is not None:
            return [field.name for field in self.model._meta.fields]
        return list(super().get_readonly_fields(request, obj))

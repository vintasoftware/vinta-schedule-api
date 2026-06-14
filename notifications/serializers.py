"""
Serializers for the notifications app.

NotificationSerializer is intentionally a plain Serializer (not ModelSerializer) so
it can handle both:
  - vintasend Notification dataclasses returned by get_in_app_unread() /
    get_in_app_notifications() (native service methods returning dataclasses).
  - vintasend_django model instances returned by ORM queries (e.g. via get_object()).

In vintasend 1.2.0 the dataclass carries: id, user_id, notification_type, title,
body_template, context_name, context_kwargs, send_after, subject_template,
preheader_template, status, context_used, created, modified — so the serializer's
timestamp fields resolve for both dataclasses and ORM rows.
"""

import logging

from django.template.exceptions import TemplateDoesNotExist
from django.template.loader import render_to_string

from rest_framework import serializers


logger = logging.getLogger(__name__)


class NotificationSerializer(serializers.Serializer):
    """
    Read-only serializer for in-app notification objects.

    Works for both vintasend Notification dataclasses (Phase 1 — returned by
    get_in_app_unread) and vintasend_django model instances (Phase 2 — ORM rows).

    Fields:
    - id, title, notification_type, status: present on both dataclass and model.
    - body: rendered at read time via body_template + best-available context.
    - created, modified: model-only; None for dataclass instances.
    """

    id = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    notification_type = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    body = serializers.SerializerMethodField()
    created = serializers.SerializerMethodField()
    modified = serializers.SerializerMethodField()

    def get_body(self, obj: object) -> str:
        """
        Render the body template with the best-available context.

        Priority:
        1. context_used — the context that was recorded at send time (on model rows,
           set by the backend when the notification was processed).
        2. context_kwargs — the original kwargs passed at creation time.
        3. Empty dict — render the template with no context (graceful degradation).

        Returns an empty string on rendering failure so the response always serialises.
        """
        body_template = getattr(obj, "body_template", "")
        if not body_template:
            return ""

        context = getattr(obj, "context_used", None) or getattr(obj, "context_kwargs", None) or {}

        try:
            return render_to_string(body_template, context)
        except TemplateDoesNotExist as exc:
            logger.warning(
                "Failed to render in-app notification body template %r: %s", body_template, exc
            )
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unexpected error rendering in-app notification body template %r: %s",
                body_template,
                exc,
            )
            return ""

    def get_created(self, obj: object) -> str | None:
        """
        Return the creation timestamp as ISO 8601 string, or None for dataclasses.

        The vintasend Notification dataclass has no `created` attribute; only the
        vintasend_django ORM model does.
        """
        created = getattr(obj, "created", None)
        if created is None:
            return None
        return created.isoformat()

    def get_modified(self, obj: object) -> str | None:
        """
        Return the last-modified timestamp as ISO 8601 string, or None for dataclasses.

        In vintasend 1.2.0 the Notification dataclass carries `modified`; ORM rows also
        have it. Returns None when the attribute is absent or None.
        """
        modified = getattr(obj, "modified", None)
        if modified is None:
            return None
        return modified.isoformat()


class BulkMarkReadSerializer(serializers.Serializer):
    """
    Input serializer for the bulk mark-as-read endpoint.

    Validates that the request body contains a non-empty list of notification ids.
    Empty list or missing field → DRF 400 validation error.
    """

    ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        max_length=100,
    )

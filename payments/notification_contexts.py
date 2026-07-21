"""Notification contexts for the dunning ladder's in-app and email notifications.

Contexts are registered via the ``@register_context`` decorator, which registers
on import. Imported from ``PaymentsConfig.ready()`` so the contexts are
registered at startup, mirroring
``calendar_integration/notification_contexts.py``.

Deliberately plain -- no branding-tree resolution like
``organizations.notification_contexts.organization_invitation_context`` -- these
values are passed in directly by ``DunningService``, which already has the
``Subscription``/``Organization`` in hand and would otherwise re-query them.
"""

from typing import Any

from vintasend.services.notification_service import register_context


@register_context("dunning_entered_grace_context")
def dunning_entered_grace_context(
    organization_name: str, grace_period_ends_at: str, **kwargs: Any
) -> dict[str, Any]:
    """Context for the notice sent once, when a subscription enters GRACE.

    Shared by both the in-app notification and the "payment failed" email --
    same facts, two renderings.
    """
    return {
        "organization_name": organization_name,
        "grace_period_ends_at": grace_period_ends_at,
        **kwargs,
    }


@register_context("dunning_reminder_context")
def dunning_reminder_context(
    organization_name: str,
    grace_period_ends_at: str,
    urgency: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the escalating reminder email ``process_dunning`` sends on each
    retry across the grace window.

    :param urgency: ``"reminder"`` while more than a day remains before
        ``grace_period_ends_at``, ``"final_warning"`` on the last day -- the
        ladder's escalation, read by the template to change its tone/subject.
    """
    return {
        "organization_name": organization_name,
        "grace_period_ends_at": grace_period_ends_at,
        "urgency": urgency,
        **kwargs,
    }


@register_context("dunning_restricted_context")
def dunning_restricted_context(organization_name: str, **kwargs: Any) -> dict[str, Any]:
    """Context for the notice sent once, when the grace period expires unresolved
    and the subscription moves to RESTRICTED."""
    return {"organization_name": organization_name, **kwargs}


@register_context("approaching_limit_context")
def approaching_limit_context(
    organization_name: str,
    resource_key: str,
    current_usage: int,
    limit_value: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the in-app notice ``UsageWarningService`` sends once per
    resource per billing cycle when usage crosses
    ``usage_warning_service.APPROACHING_LIMIT_THRESHOLD`` (default 80%) of the
    resource's effective limit, without yet being at or over it."""
    return {
        "organization_name": organization_name,
        "resource_key": resource_key,
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }


@register_context("limit_reached_context")
def limit_reached_context(
    organization_name: str,
    resource_key: str,
    current_usage: int,
    limit_value: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the in-app notice ``UsageWarningService`` sends once per
    resource per billing cycle once usage is at or over the resource's
    effective limit."""
    return {
        "organization_name": organization_name,
        "resource_key": resource_key,
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }

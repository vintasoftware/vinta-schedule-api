"""Every builder here takes ``scope_name`` and emits ``organization_name``.

That asymmetry is deliberate and is the whole 0.8.0 adaptation for this module.
``vinta-django-billing`` 0.8.0 renamed the kwarg it passes -- a scope may name
something that is not an organization, so it sends the payer's ``label`` under a
neutral name. Every payer in *this* project is an organization, and the
templates under ``templates/payments/emails/`` have said ``{{ organization_name }}``
since before the billing engine was extracted. Renaming the parameter follows
the engine; renaming the context key would mean editing a dozen translated
templates to say the same thing in a less specific way.

Notification contexts for the dunning ladder's in-app and email notifications.

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

from vinta_billing.registry import resources
from vintasend.services.notification_service import register_context


@register_context("dunning_entered_grace_context")
def dunning_entered_grace_context(
    scope_name: str, grace_period_ends_at: str, **kwargs: Any
) -> dict[str, Any]:
    """Context for the notice sent once, when a subscription enters GRACE.

    Shared by both the in-app notification and the "payment failed" email --
    same facts, two renderings.
    """
    return {
        "organization_name": scope_name,
        "grace_period_ends_at": grace_period_ends_at,
        **kwargs,
    }


@register_context("dunning_reminder_context")
def dunning_reminder_context(
    scope_name: str,
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
        "organization_name": scope_name,
        "grace_period_ends_at": grace_period_ends_at,
        "urgency": urgency,
        **kwargs,
    }


@register_context("dunning_restricted_context")
def dunning_restricted_context(scope_name: str, **kwargs: Any) -> dict[str, Any]:
    """Context for the notice sent once, when the grace period expires unresolved
    and the subscription moves to RESTRICTED."""
    return {"organization_name": scope_name, **kwargs}


@register_context("approaching_limit_context")
def approaching_limit_context(
    scope_name: str,
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
        "organization_name": scope_name,
        "resource_key": resource_key,
        "resource_label": resources.get(resource_key).label,
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }


@register_context("limit_reached_context")
def limit_reached_context(
    scope_name: str,
    resource_key: str,
    current_usage: int,
    limit_value: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Context for the in-app notice ``UsageWarningService`` sends once per
    resource per billing cycle once usage is at or over the resource's
    effective limit."""
    return {
        "organization_name": scope_name,
        "resource_key": resource_key,
        "resource_label": resources.get(resource_key).label,
        "current_usage": current_usage,
        "limit_value": limit_value,
        **kwargs,
    }

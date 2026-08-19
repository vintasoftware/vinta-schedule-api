"""Transitional shim over ``vinta_billing.constants`` -- and the last home of the
two enums the package deliberately has no counterpart for.

Seven of the nine classes this module used to define moved to the package
unchanged and are re-exported below. ``LimitedResource`` and ``Entitlement`` did
not: section 3.3 of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``
turns them into *registrations* against ``vinta_billing.registry`` (see
``payments/seams/resources.py``, which is their definition site now and writes
its keys and labels as its own literals). A closed enum of "things this product
sells" is exactly what a billing library cannot own, so there is nothing to
import them from -- they stay defined here, verbatim, for the consumers that
still name them: ``organizations/``, ``calendar_integration/``, ``webhooks/``,
``public_api/``, and migrations ``0007`` and ``0021``.

Phases 3 and 4 retarget those consumers at registry keys; **Phase 6 then deletes
this module entirely**, including the two class definitions below.
"""

from django.db.models import TextChoices
from django.utils.translation import gettext as _

from vinta_billing.constants import (
    BillingInterval,
    BillingState,
    DocumentTypes,
    LimitKind,
    LimitRemedy,
    LimitWarningLevel,
    ProviderWebhookRoute,
)


__all__ = [
    "BillingInterval",
    "BillingState",
    "DocumentTypes",
    "Entitlement",
    "LimitKind",
    "LimitRemedy",
    "LimitWarningLevel",
    "LimitedResource",
    "ProviderWebhookRoute",
]


class LimitedResource(TextChoices):
    """The closed set of resources a ``BillingPlan`` can put a ceiling on.

    Adding a member here is the only way a new resource enters the limits system —
    the ``unlimited`` plan seed enumerates this class dynamically (see the seed data
    migration + ``test_plan_seed_migration.py``) so a new member can never be silently
    missing a ``PlanLimit`` row on the rollback plan.
    """

    ORGANIZATION_MEMBERS = ("organization_members", _("Organization members"))
    RESOURCE_CALENDARS = ("resource_calendars", _("Resource calendars"))
    CALENDAR_GROUPS = ("calendar_groups", _("Calendar groups"))
    BUNDLE_CALENDARS = ("bundle_calendars", _("Bundle calendars"))
    AVAILABILITY_WINDOWS = ("availability_windows", _("Availability windows"))
    WEBHOOK_SUBSCRIPTIONS = ("webhook_subscriptions", _("Webhook subscriptions"))
    PUBLIC_API_SYSTEM_USERS = ("public_api_system_users", _("Public API system users"))
    EVENT_OCCURRENCES = ("event_occurrences", _("Event occurrences"))


class Entitlement(TextChoices):
    """The closed set of boolean feature gates a ``BillingPlan`` can grant."""

    EXTERNAL_CALENDAR_GOOGLE = ("external_calendar_google", _("Google Calendar sync"))
    EXTERNAL_CALENDAR_MICROSOFT = ("external_calendar_microsoft", _("Microsoft Calendar sync"))
    PARTNER_API = ("partner_api", _("Partner / public API access"))
    WHITE_LABEL_BRANDING = ("white_label_branding", _("White-label branding"))
    ADVANCED_SCHEDULING = ("advanced_scheduling", _("Advanced scheduling"))

from django.db.models import TextChoices
from django.utils.translation import gettext as _


class BillingState(TextChoices):
    """Billing lifecycle state of an organization's ``Subscription``.

    The spec's lifecycle diagram is the authority on the transitions between
    these states (see the Billing Plans and Limits spec).
    """

    FREE = ("free", _("Free"))
    ACTIVE = ("active", _("Active"))
    GRACE = ("grace", _("Grace period"))
    RESTRICTED = ("restricted", _("Restricted"))
    CANCELLED = ("cancelled", _("Cancelled"))


class BillingInterval(TextChoices):
    """Billing cadence for a ``Subscription``."""

    MONTHLY = ("monthly", _("Monthly"))
    ANNUAL = ("annual", _("Annual"))


class ProviderWebhookRoute(TextChoices):
    """Which inbound webhook endpoint received a ``ProviderWebhookEvent``.

    Scopes the idempotency ledger's uniqueness alongside ``provider`` and
    ``external_event_id`` — a provider's event-id numbering is not guaranteed to be
    disjoint between its payment and subscription-payment notification streams.
    """

    PAYMENT_UPDATE = ("payment_update", _("Payment update"))
    SUBSCRIPTION_PAYMENT_UPDATE = ("subscription_payment_update", _("Subscription payment update"))


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


class LimitKind(TextChoices):
    """Whether a ``LimitedResource`` is capped up front or metered and billed after
    the fact."""

    PREPAID = ("prepaid", _("Prepaid"))
    POSTPAID = ("postpaid", _("Postpaid"))


class Entitlement(TextChoices):
    """The closed set of boolean feature gates a ``BillingPlan`` can grant."""

    EXTERNAL_CALENDAR_GOOGLE = ("external_calendar_google", _("Google Calendar sync"))
    EXTERNAL_CALENDAR_MICROSOFT = ("external_calendar_microsoft", _("Microsoft Calendar sync"))
    PARTNER_API = ("partner_api", _("Partner / public API access"))
    WHITE_LABEL_BRANDING = ("white_label_branding", _("White-label branding"))
    ADVANCED_SCHEDULING = ("advanced_scheduling", _("Advanced scheduling"))

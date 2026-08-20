"""The resource and entitlement registry keys, as plain string constants.

**Zero imports, deliberately.** This is the one property that makes this module
safe to import from anywhere, including from inside another app's `models.py`:
`payments/seams/resources.py` (the registration site, which builds counters out
of `calendar_integration`, `organizations`, `public_api` and `webhooks` models)
cannot be that safe import, because a module in any of those four apps that
wants a key back would form a direct import cycle with it. This module holds
nothing but the keys, so nothing can cycle through it.

The concrete cycle this module exists to break: ``organizations/models.py``
needs ``WHITE_LABEL_BRANDING`` to gate branding, but ``payments/seams/
resources.py`` imports ``organizations.models`` to build the
``organization_members`` counter. Before this module existed, the only fix was
a deferred, function-body import inside ``organizations/models.py`` (see that
module's git history) -- workable, but a symptom: any future app that wants a
resource key back from ``resources.py`` would hit the identical cycle and need
the identical workaround. Importing this module instead makes the cycle
structurally impossible rather than merely avoided.

The keys and labels are also defined as literals in
``payments/migrations/0007_seed_billing_plans.py``, deliberately **not**
imported from here -- see that migration's module docstring for why a data
migration keeps its own frozen copy rather than importing a live module,
including this one.

``payments/seams/resources.py`` imports these to build every
``resources.register(...)`` / ``entitlements.register(...)`` call; every other
consumer that only needs a symbol should import it from here directly rather
than through ``resources.py``, which pulls in `calendar_integration`,
`organizations`, `public_api` and `webhooks` models as an import-time side
effect.
"""

from __future__ import annotations


#: The eight registered resource keys. The strings themselves are the
#: definition -- they are what the ``PlanLimit`` / ``BillingPeriodResourceUsage``
#: rows already hold and what the API already returns -- but a call site should
#: still say what it means rather than repeat a literal that nothing would flag
#: if it were mistyped.
ORGANIZATION_MEMBERS = "organization_members"
RESOURCE_CALENDARS = "resource_calendars"
CALENDAR_GROUPS = "calendar_groups"
BUNDLE_CALENDARS = "bundle_calendars"
AVAILABILITY_WINDOWS = "availability_windows"
WEBHOOK_SUBSCRIPTIONS = "webhook_subscriptions"
PUBLIC_API_SYSTEM_USERS = "public_api_system_users"
EVENT_OCCURRENCES = "event_occurrences"

#: The five registered entitlement keys -- same rationale as the resource keys
#: above.
EXTERNAL_CALENDAR_GOOGLE = "external_calendar_google"
EXTERNAL_CALENDAR_MICROSOFT = "external_calendar_microsoft"
PARTNER_API = "partner_api"
WHITE_LABEL_BRANDING = "white_label_branding"
ADVANCED_SCHEDULING = "advanced_scheduling"

#: Every resource / entitlement key, grouped -- for call sites that want "all of
#: them" rather than one at a time (e.g. iterating to seed a `PlanLimit` row per
#: resource). Order matches `payments/seams/resources.py`'s registration order.
RESOURCE_KEYS = (
    ORGANIZATION_MEMBERS,
    RESOURCE_CALENDARS,
    CALENDAR_GROUPS,
    BUNDLE_CALENDARS,
    AVAILABILITY_WINDOWS,
    WEBHOOK_SUBSCRIPTIONS,
    PUBLIC_API_SYSTEM_USERS,
    EVENT_OCCURRENCES,
)
ENTITLEMENT_KEYS = (
    EXTERNAL_CALENDAR_GOOGLE,
    EXTERNAL_CALENDAR_MICROSOFT,
    PARTNER_API,
    WHITE_LABEL_BRANDING,
    ADVANCED_SCHEDULING,
)

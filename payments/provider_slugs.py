"""Transitional re-export of ``vinta_billing.provider_slugs``.

The slugs are plain strings that both the host settings module and the package's
adapters need *before* the app registry is ready, which is why they live in a
module of their own on both sides rather than on the ``PaymentProviders`` enum.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``;
``vinta_schedule_api/settings/base.py`` is retargeted in Phase 2.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.provider_slugs import *  # noqa: F403

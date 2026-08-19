"""Transitional re-export of ``vinta_billing.entitlement_cache``.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``;
``organizations`` and ``public_api`` are retargeted in Phases 3 and 4.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.entitlement_cache import *  # noqa: F403

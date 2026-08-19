"""Transitional re-export of ``vinta_billing.services.entitlement_service``.

``USAGE_COUNTERS`` is gone rather than re-exported: the closed dict of
``resource_key -> counter`` it was is now the open registry at
``vinta_billing.registry.resources``, which ``payments/seams/resources.py``
populates, and there is nothing dict-shaped to import in its place.
``UsageContext`` survives -- as ``vinta_billing.counting.UsageContext``, which
the package module imports and this star import therefore re-exports -- but it
no longer carries a dedicated ``exclude_invitation_id`` field; per-call counter
data travels through the opaque ``extra`` mapping instead.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.services.entitlement_service import *  # noqa: F403

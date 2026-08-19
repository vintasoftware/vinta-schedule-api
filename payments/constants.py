"""Transitional re-export of the payment-provider / status enums from
``vinta_billing.constants``.

All four classes moved to the package byte-for-byte -- same members, same stored
values, same labels -- so this module defines nothing and only keeps the
``from payments.constants import ...`` spelling alive for consumers the later
phases have not retargeted yet.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.constants import *  # noqa: F403

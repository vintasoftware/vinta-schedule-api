"""Transitional re-export of ``vinta_billing.admin``.

Importing the package module is also what registers every billing ``ModelAdmin``
with the default admin site, so ``django.contrib.admin.autodiscover``'s import of
``payments.admin`` keeps having the same effect it had before the engine moved.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.admin import *  # noqa: F403

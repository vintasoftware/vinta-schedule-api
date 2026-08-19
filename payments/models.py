"""Transitional re-export of ``vinta_billing.models``.

Every billing model is the package's from
``payments/migrations/0024_move_billing_to_vinta_billing.py`` onward -- the rows
live in ``vinta_billing_*`` tables under the ``vinta_billing`` app label, and
this module defines nothing. It exists only so the consumers that still say
``from payments.models import ...`` keep working while the remaining phases
retarget them one app at a time.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``,
once no importer names it.

Re-exporting a model class does *not* register a second Django model: Django
registers a model against the app that owns the module it is *defined* in, so
``payments`` is model-free from here on and ``makemigrations`` has nothing to
generate for it.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.models import *  # noqa: F403

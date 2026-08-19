"""Transitional re-export of ``vinta_billing.exceptions``.

All twenty-six exceptions this module used to define now live in the package.
Twenty-five moved unchanged -- same class names, same ``code`` discriminators,
same rendered bodies.

The twenty-sixth, ``InapplicableInvitationExclusionError``, was replaced rather
than moved. It fired when ``exclude_invitation_id`` was passed for a resource
whose usage counter does not read it, which as of ``vinta-django-billing``
0.4.0 is the general case the package raises
``InapplicableUsageExtraError`` for: a resource declares the ``usage_extra``
keys its counter reads (``payments.seams.resources`` declares
``{"exclude_invitation_id"}`` on ``organization_members`` and an explicitly
empty set on the other seven), and a key outside that declaration raises from
``check_limit``, ``get_current_usage`` and ``get_usage_breakdown`` alike. Same
guard, same ``BillingError`` base -- so it is still not a ``ValueError`` that
the ``except ValueError`` wrappers around service calls could flatten into a
user-facing validation message -- and a message that also names which keys were
unexpected and which the resource declared. The host name is gone rather than
kept as an alias: an exception nothing raises is a name that outlives its
behaviour.

**Removed in Phase 6** of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``.

The star import is deliberate: the module this replaces also re-exported
whatever it imported, and callers relied on that. Naming a subset here would
silently narrow the shim's surface.
"""

from vinta_billing.exceptions import *  # noqa: F403

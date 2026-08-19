"""The host's configuration of the ``vinta_billing`` engine.

Everything under this package is one of the five seams a generic billing
package cannot own by itself: which resources and entitlements exist, whose
subscription pays for whom, where dunning/usage messages go, what a "billable
occurrence" is, and how a per-subscription job reaches the existing Celery
queue. See ``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``
for the full rationale.

Nothing here is imported yet outside this package and its tests -- ``payments``
does not register these seams from ``AppConfig.ready()`` until the app that
needs them (``vinta_billing``) is actually installed, which is Phase 1.
"""

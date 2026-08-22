"""The host's configuration of the ``vinta_billing`` engine.

Everything under this package is one of the seams a generic billing
package cannot own by itself: which resources and entitlements exist, whose
subscription pays for whom, where dunning/usage messages go, and what a
"billable occurrence" is. See ``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``
for the full rationale.

Every submodule here is already imported at process start, today: ``payments``
is in ``INTERNAL_INSTALLED_APPS``, and ``di_core.apps.DICoreConfig.ready()``
wires the DI container over every package in that list
(``container.wire(packages=INTERNAL_INSTALLED_APPS)``), which recursively
imports each one's submodules -- these five included -- before
``payments.apps.PaymentsConfig.ready()`` even runs. Phase 1 adds an explicit
import of these seams from ``PaymentsConfig.ready()`` once the app that needs
them (``vinta_billing``) is actually installed; that import documents the
dependency on purpose, but registration itself does not wait for it.
"""

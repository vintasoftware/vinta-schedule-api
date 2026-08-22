"""An app registry that answers ``payments.<BillingModel>`` after the move.

Several modules here test a historical data migration by calling its
``RunPython`` function directly with the *live* registry
(``django.apps.apps``) rather than the historical one a real ``RunPython`` step
receives. Each of them says so, and says why: the models those migrations touch
had the same shape at their point in the graph as they do now, so the live
classes behave identically for every operation the migration performs.
``payments/tests/test_plan_seed_migration.py`` set the precedent; the others
cite it.

Each of them also names the condition under which the shortcut stops being
safe -- "if a future migration changes one of those models' shape". ``payments/
migrations/0024_move_billing_to_vinta_billing.py`` is that migration, in the
strongest form: it does not reshape the models, it moves them to another app
label, so ``apps.get_model("payments", "BillingPlan")`` raises ``LookupError``
against the live registry while still resolving perfectly well inside the real
migration (``0009`` runs long before ``0024``, at a state where ``payments``
still owns every billing model).

What broke is therefore the shortcut, not the migration. This restores the
shortcut rather than the shape: the *same live classes*, reachable under the
label their migration asks for.

Rebuilding a genuine historical state instead was the alternative and is worse
here. ``MigrationExecutor``-rendered models at ``payments/0008`` point at
``payments_*`` tables, which no longer exist, so every query in these tests
would fail against a database that is correct; and driving a real reverse to
get those tables back is a twenty-table round trip per test.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps as live_apps
from django.db.models import Model


#: The label the billing models live under now.
BILLING_APP_LABEL = "vinta_billing"

#: What ``payments`` still owns. Nothing, model-wise -- the app is
#: configuration from ``0024`` onward -- so every model lookup against it is
#: redirected. Spelled out anyway: if ``payments`` ever gains a model of its
#: own, a lookup for it must not be silently answered by another app.
PAYMENTS_OWN_MODELS: frozenset[str] = frozenset()


class HistoricalBillingApps:
    """``django.apps.apps``, with billing models reachable as ``payments.*``.

    Only ``get_model`` is implemented, because that is the only thing a
    ``RunPython`` function is given the registry for. An attribute a migration
    reaches for and this does not have raises ``AttributeError`` rather than
    being forwarded blindly -- forwarding would let a test quietly exercise a
    path the real historical registry could not support.
    """

    def get_model(self, app_label: str, model_name: str | None = None) -> type[Model]:
        if model_name is None:
            app_label, model_name = app_label.split(".")
        if app_label == "payments" and model_name.lower() not in PAYMENTS_OWN_MODELS:
            return live_apps.get_model(BILLING_APP_LABEL, model_name)
        return live_apps.get_model(app_label, model_name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"{type(self).__name__} implements get_model() only; a migration reaching "
            f"for {name!r} needs a real historical registry, not this stand-in."
        )


#: Pass this where a ``RunPython`` function expects ``apps``.
historical_apps = HistoricalBillingApps()

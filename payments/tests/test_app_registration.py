"""What survives once ``payments/`` holds no re-export shims (Phase 6 of
``ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md``).

Supersedes ``payments/tests/test_shims.py``, whose whole premise -- that
``payments.models`` / ``payments.exceptions`` / ``payments.services.*`` etc. were
one-line re-exports of their ``vinta_billing`` counterparts -- stopped holding
once those modules were deleted. What is left worth asserting:

1. **``payments`` still registers no models.** True since the table-move
   migration (``0024``), unrelated to the shims' existence, so it survives them.
2. **``vinta_billing`` owns all twenty billing models.**
3. **The registered ``BillingProfile`` admin is the package's own.**
4. **The invitation-exclusion guard lives in the package now, under a new name.**
   ``InapplicableInvitationExclusionError`` fired when ``exclude_invitation_id``
   was aimed at a resource whose counter does not read it.
   ``vinta_billing.exceptions.InapplicableUsageExtraError`` is the general form,
   raised off what a resource declares (``ResourceDefinition.usage_extra_keys``).
"""

from django.apps import apps

from vinta_billing.exceptions import BillingError, InapplicableUsageExtraError
from vinta_billing.registry import resources


def test_the_registered_billing_profile_admin_is_the_packages_own():
    """No host subclass sits in front of it (Phase 2 deleted
    ``payments/admin.py``): the package's own ``BillingProfileAdmin`` --
    ``save_model`` resolving ``subscription_service`` through
    ``VINTA_BILLING['SERVICE_CONTAINER']`` since 0.5.0 -- is what
    ``django.contrib.admin.autodiscover()`` registered for
    ``vinta_billing.models.BillingProfile``.
    """
    from django.contrib import admin as django_admin

    import vinta_billing.admin
    from vinta_billing.models import BillingProfile

    registered = django_admin.site._registry[BillingProfile]

    assert type(registered) is vinta_billing.admin.BillingProfileAdmin


def test_payments_registers_no_models():
    """Django binds a model to the app that owns the module it is *defined* in.
    Were that not so, ``makemigrations`` would want a table for a
    ``payments.Subscription`` that does not exist."""
    assert list(apps.get_app_config("payments").get_models()) == []


def test_vinta_billing_owns_the_twenty_billing_models():
    """The other half of the same statement: the models are somewhere, and that
    somewhere is the package's app label."""
    model_names = {model.__name__ for model in apps.get_app_config("vinta_billing").get_models()}

    assert len(model_names) == 20
    assert {"Subscription", "BillingPlan", "BillingProfile", "MeteredOccurrence"} <= model_names


def test_the_invitation_exclusion_guard_moved_rather_than_disappeared():
    """``InapplicableUsageExtraError`` is still a ``BillingError`` -- so it is not
    a ``ValueError`` that an ``except ValueError`` wrapper around a service call
    could flatten into a generic validation message -- and every registered
    resource declares its ``usage_extra_keys`` (the package's undeclared default,
    ``None``, would turn the guard off for that resource)."""
    assert issubclass(InapplicableUsageExtraError, BillingError)
    undeclared = [d.key for d in resources if d.usage_extra_keys is None]
    assert undeclared == [], (
        f"{undeclared} declare no usage_extra keys, so the guard is off for them"
    )

"""``VINTA_BILLING`` -- every dotted-path seam resolves, and the scalars carry
the values the plan promises.

``vinta_billing`` is not in ``INSTALLED_APPS`` yet (Phase 1), so this test
resolves each setting directly through ``vinta_billing.conf`` rather than by
exercising a live package code path -- there is no package code path to
exercise yet. It exists so a typo in ``settings/base.py`` fails here, at unit
speed, rather than being discovered the first time a later phase touches the
setting it broke.
"""

from django.conf import settings
from django.test import override_settings

import pytest
from vinta_billing.conf import get_object_from_setting, get_setting

from payments.provider_slugs import PAYMENT_PROVIDER_SLUGS


#: Every ``VINTA_BILLING`` key this project configures with a dotted import
#: path -- i.e. every key resolved through ``get_object_from_setting`` rather
#: than read as a plain scalar. ``OCCURRENCE_SOURCE`` is included even though
#: the package treats it as optional (``None`` is a legal, no-op value):
#: Phase 0 sets it, so it belongs on the "must resolve" list here too.
DOTTED_PATH_KEYS = (
    "HIERARCHY",
    "BILLING_MANAGER_PREDICATE",
    "NOTIFIER",
    "OCCURRENCE_SOURCE",
    "BILLING_RECIPIENTS",
    "JOB_DISPATCHER",
)


class TestVintaBillingSettingsResolve:
    @pytest.mark.parametrize("key", DOTTED_PATH_KEYS)
    def test_dotted_path_setting_resolves(self, key):
        """Every dotted-path key in ``VINTA_BILLING`` must import cleanly.

        ``get_object_from_setting`` is exactly what the package itself calls
        the first time it needs the object -- resolving it here at unit speed
        is what stops a typo from surfacing only once a later phase's code
        path actually calls into the seam.
        """
        resolved = get_object_from_setting(key)
        assert resolved is not None, f"VINTA_BILLING[{key!r}] resolved to None"

    def test_unknown_key_is_rejected(self):
        """``vinta_billing.conf._build_settings`` raises on an unknown key --
        pinning that here means a future typo in ``settings/base.py`` fails
        loudly at first ``get_setting`` access rather than silently falling
        back to the package's own default."""
        with (
            override_settings(VINTA_BILLING={**settings.VINTA_BILLING, "NOT_A_REAL_KEY": True}),
            pytest.raises(ValueError, match="Unknown VINTA_BILLING key"),
        ):
            get_setting("HIERARCHY")


class TestUrlNamespace:
    def test_url_namespace_is_api_not_the_package_default(self):
        """MUST be ``"api"``, not the package's own default (``"billing"``).

        ``vinta_billing``'s MercadoPago adapters reverse their own webhook
        callback URLs through ``URL_NAMESPACE`` (``vinta_billing/urls_helpers.py``),
        and this project mounts the shared DRF router as
        ``include((router.urls, "api"))`` (``vinta_schedule_api/urls.py``) --
        exactly what the host's own (pre-migration) MercadoPago adapter already
        hardcodes reversing under today
        (``payments/services/payment_adapters/mercadopago_payment_adapter.py``).
        Left at the package default, MercadoPago webhook-callback URL
        construction raises ``NoReverseMatch`` -- and only once MercadoPago is
        actually exercised, so a green suite would not catch a wrong value
        here on its own.
        """
        assert get_setting("URL_NAMESPACE") == "api"


class TestProviders:
    def test_providers_carries_a_key_per_payment_provider_slug(self):
        providers = get_setting("PROVIDERS")
        assert set(providers) == set(PAYMENT_PROVIDER_SLUGS)

    def test_default_provider_is_a_valid_provider_slug(self):
        assert get_setting("DEFAULT_PROVIDER") in PAYMENT_PROVIDER_SLUGS


class TestScalarSettings:
    def test_metered_resource_key_is_event_occurrences(self):
        assert get_setting("METERED_RESOURCE_KEY") == "event_occurrences"

    def test_grace_period_days_matches_the_hosts_existing_fallback(self):
        assert get_setting("GRACE_PERIOD_DAYS") == settings.BILLING_DEFAULT_GRACE_PERIOD_DAYS

    def test_usage_warning_threshold_matches_the_hosts_existing_value(self):
        """Pinned to 0.8 -- the value ``usage_warning_service
        .APPROACHING_LIMIT_THRESHOLD`` already enforces today, so wiring this
        setting in a later phase changes nothing observable."""
        assert get_setting("USAGE_WARNING_THRESHOLD") == 0.8

    def test_default_currency_is_usd(self):
        assert get_setting("DEFAULT_CURRENCY") == "USD"

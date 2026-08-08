"""Tests for payment settings configuration."""

import os
import subprocess
import sys

from django.conf import settings

from payments.constants import PaymentProviders
from payments.provider_slugs import PAYMENT_PROVIDER_SLUGS


class TestDefaultPaymentProviderSetting:
    """Test DEFAULT_PAYMENT_PROVIDER setting validation."""

    def test_default_payment_provider_is_valid(self) -> None:
        """DEFAULT_PAYMENT_PROVIDER must be a valid PaymentProviders member."""
        assert settings.DEFAULT_PAYMENT_PROVIDER in PaymentProviders.values

    def test_default_payment_provider_is_stripe(self) -> None:
        """System default provider should be stripe."""
        assert settings.DEFAULT_PAYMENT_PROVIDER == PaymentProviders.STRIPE

    def test_invalid_payment_provider_raises_improperly_configured(self) -> None:
        """An invalid DEFAULT_PAYMENT_PROVIDER value fails `manage.py check --deploy`.

        Settings validation happens at import time and is process-global, so the only
        way to actually exercise the failure is out-of-process: spawn `manage.py check
        --deploy` with a bogus DEFAULT_PAYMENT_PROVIDER and assert it fails with
        ImproperlyConfigured.
        """
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "vinta_schedule_api.settings.test",
            "DEFAULT_PAYMENT_PROVIDER": "nonsense",
        }
        result = subprocess.run(  # noqa: S603
            [sys.executable, "manage.py", "check", "--deploy"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode != 0
        assert "ImproperlyConfigured" in result.stderr

    def test_payment_providers_values_match_settings(self) -> None:
        """PaymentProviders.values must match the real PAYMENT_PROVIDER_SLUGS tuple.

        settings/base.py validates DEFAULT_PAYMENT_PROVIDER against
        payments.provider_slugs.PAYMENT_PROVIDER_SLUGS (a Django-import-free leaf
        module, to avoid an import cycle -- see that module's docstring).
        payments.constants.PaymentProviders binds its member values to the same
        constants, so this test compares the two real objects directly rather than a
        hand-copied literal.
        """
        assert set(PaymentProviders.values) == set(PAYMENT_PROVIDER_SLUGS)

"""Phase 6 — ``ACCOUNT_PHONE_VERIFICATION_ENABLED`` is env-driven, default off.

Unit-level: exercises the exact ``decouple.config(...)`` expression used in
``vinta_schedule_api/settings/base.py`` directly, so it fails if the cast/
default behavior ever regresses (e.g. someone swaps ``cast=bool`` for a plain
string compare). Does not reload the settings module — that would have wider
side effects (app registry, other required env vars) unrelated to this
single setting's resolution.
"""

from django.conf import settings
from django.test import override_settings

import pytest
from decouple import config


class TestAccountPhoneVerificationEnabledEnvVarResolution:
    def test_defaults_to_false_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACCOUNT_PHONE_VERIFICATION_ENABLED", raising=False)

        resolved = config("ACCOUNT_PHONE_VERIFICATION_ENABLED", cast=bool, default=False)

        assert resolved is False

    @pytest.mark.parametrize("truthy_value", ["true", "True", "1", "yes", "on"])
    def test_resolves_true_for_truthy_env_values(
        self, monkeypatch: pytest.MonkeyPatch, truthy_value: str
    ) -> None:
        monkeypatch.setenv("ACCOUNT_PHONE_VERIFICATION_ENABLED", truthy_value)

        resolved = config("ACCOUNT_PHONE_VERIFICATION_ENABLED", cast=bool, default=False)

        assert resolved is True

    @pytest.mark.parametrize("falsy_value", ["false", "False", "0", "no", "off"])
    def test_resolves_false_for_falsy_env_values(
        self, monkeypatch: pytest.MonkeyPatch, falsy_value: str
    ) -> None:
        monkeypatch.setenv("ACCOUNT_PHONE_VERIFICATION_ENABLED", falsy_value)

        resolved = config("ACCOUNT_PHONE_VERIFICATION_ENABLED", cast=bool, default=False)

        assert resolved is False


class TestAccountPhoneVerificationEnabledDefaultSetting:
    """Behavioral: the setting object itself, as loaded for the test run."""

    def test_default_is_false_with_no_override(self) -> None:
        assert settings.ACCOUNT_PHONE_VERIFICATION_ENABLED is False

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_can_be_overridden_true_per_environment(self) -> None:
        assert settings.ACCOUNT_PHONE_VERIFICATION_ENABLED is True

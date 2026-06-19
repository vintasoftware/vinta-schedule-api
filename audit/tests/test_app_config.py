"""Tests for audit app configuration."""

from django.apps import apps


def test_audit_app_config_resolves() -> None:
    """Test that the audit app config resolves correctly."""
    app_config = apps.get_app_config("audit")
    assert app_config.name == "audit"


def test_audit_app_imports_cleanly() -> None:
    """Test that the audit app imports without errors."""
    import audit  # noqa: F401
    from audit import admin, apps, models  # noqa: F401

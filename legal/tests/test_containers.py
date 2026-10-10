"""Tests for legal.containers."""

from di_core.containers import AppContainer
from legal.containers import LegalContainer


class TestLegalContainerProviders:
    """Test that legal providers are properly wired in LegalContainer."""

    def test_consent_service_identity(self):
        """Provider alias is correctly set."""
        assert AppContainer.consent_service is LegalContainer.consent_service

    def test_consent_service_has_audit_service_dependency(self):
        """consent_service is wired with audit_service as a dependency."""
        container = AppContainer()
        consent = container.consent_service()
        audit = container.audit_service()
        assert consent is not None
        assert audit is not None
        assert hasattr(consent, "audit_service")
        assert consent.audit_service is not None

"""Tests for legal.containers."""

from di_core.containers import AppContainer
from legal.containers import LegalContainer


class TestLegalContainerProviders:
    """Verify that legal providers moved from AppContainer to LegalContainer."""

    def test_consent_service_identity(self):
        """AppContainer.consent_service is an alias to LegalContainer.consent_service."""
        assert AppContainer.consent_service is LegalContainer.consent_service

    def test_consent_service_has_audit_service_dependency(self):
        """consent_service is properly wired with audit_service."""
        container = AppContainer()
        consent = container.consent_service()
        audit = container.audit_service()
        # Both should be properly instantiated
        assert consent is not None
        assert audit is not None
        # consent_service should have the audit_service attribute
        assert hasattr(consent, "audit_service")
        assert consent.audit_service is not None

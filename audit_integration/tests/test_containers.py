"""Tests for audit_integration.containers."""

from audit_integration.containers import AuditContainer
from di_core.containers import AppContainer


class TestAuditContainerProviders:
    """Verify that audit providers moved from AppContainer to AuditContainer."""

    def test_audit_service_identity(self):
        """AppContainer.audit_service is an alias to AuditContainer.audit_service."""
        assert AppContainer.audit_service is AuditContainer.audit_service

    def test_audit_repository_identity(self):
        """AppContainer.audit_repository is an alias to AuditContainer.audit_repository."""
        assert AppContainer.audit_repository is AuditContainer.audit_repository

    def test_audit_additional_repositories_identity(self):
        """AppContainer.audit_additional_repositories is an alias to AuditContainer.audit_additional_repositories."""
        assert (
            AppContainer.audit_additional_repositories
            is AuditContainer.audit_additional_repositories
        )

    def test_singleton_repository_across_resolutions(self):
        """Built AppContainer() resolves audit_repository as a Singleton across multiple calls."""
        container = AppContainer()
        repo1 = container.audit_repository()
        repo2 = container.audit_repository()
        assert repo1 is repo2

    def test_audit_service_uses_singleton_repository(self):
        """audit_service receives the singleton audit_repository instance."""
        container = AppContainer()
        service = container.audit_service()
        repo_direct = container.audit_repository()
        assert service.repository is repo_direct

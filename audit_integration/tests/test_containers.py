"""Tests for audit_integration.containers."""

from audit_integration.containers import AuditContainer
from di_core.containers import AppContainer


class TestAuditContainerProviders:
    """Verify that audit providers moved from AppContainer to AuditContainer."""

    def test_audit_service_identity(self) -> None:
        """AppContainer.audit_service is an alias to AuditContainer.audit_service."""
        assert AppContainer.audit_service is AuditContainer.audit_service

    def test_audit_repository_identity(self) -> None:
        """AppContainer.audit_repository is an alias to AuditContainer.audit_repository."""
        assert AppContainer.audit_repository is AuditContainer.audit_repository

    def test_audit_additional_repositories_identity(self) -> None:
        """Alias identity for audit_additional_repositories."""
        assert (
            AppContainer.audit_additional_repositories
            is AuditContainer.audit_additional_repositories
        )

    def test_singleton_repository_across_resolutions(self) -> None:
        """Built AppContainer() resolves audit_repository as a Singleton across multiple calls."""
        container = AppContainer()
        assert container.audit_repository() is container.audit_repository()

    def test_audit_service_shares_singleton_repository_across_resolutions(self) -> None:
        """Two audit_service() resolutions are distinct but share the one repository."""
        container = AppContainer()
        first = container.audit_service()
        second = container.audit_service()
        assert first is not second
        assert first.repository is second.repository is container.audit_repository()

    def test_audit_service_receives_additional_repositories(self) -> None:
        """audit_service is wired with the (empty) additional repositories mapping."""
        container = AppContainer()
        assert container.audit_additional_repositories() == {}
        assert container.audit_service().additional_repositories == {}

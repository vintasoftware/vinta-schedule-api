"""Tests for audit_integration.containers."""

from dependency_injector import providers

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
        """audit_repository is a Singleton on a built AppContainer."""
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
        """audit_service is wired with whatever audit_additional_repositories provides."""
        container = AppContainer()
        extra = object()
        with container.audit_additional_repositories.override(providers.Dict(extra=extra)):
            assert container.audit_service().additional_repositories == {"extra": extra}

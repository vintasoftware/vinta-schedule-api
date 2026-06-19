"""Tests for audit DI container wiring."""

from __future__ import annotations

from audit.repositories import DjangoORMAuditRepository
from di_core.containers import AppContainer


class TestAuditRepositoryWiring:
    """Tests for audit_repository provider in the DI container."""

    def test_audit_repository_resolves(self):
        """The audit_repository provider resolves to a DjangoORMAuditRepository instance."""
        container = AppContainer()
        repository = container.audit_repository()
        assert isinstance(repository, DjangoORMAuditRepository)

    def test_audit_repository_is_singleton(self):
        """The audit_repository provider is a Singleton (same instance on multiple resolutions)."""
        container = AppContainer()
        repo1 = container.audit_repository()
        repo2 = container.audit_repository()
        assert repo1 is repo2

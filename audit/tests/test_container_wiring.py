"""Tests for audit DI container wiring."""

from __future__ import annotations

import di_core.containers
from audit.repositories import DjangoORMAuditRepository
from audit.services import AuditService
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


class TestAuditServiceWiring:
    """Tests for audit_service provider in the DI container.

    Uses the wired global container (di_core.containers.container) so that the
    @inject decorator on AuditService.__init__ can resolve audit_repository.
    """

    def test_audit_service_resolves(self):
        """audit_service() resolves to an AuditService with a non-None, injected repository."""
        container = di_core.containers.container
        svc = container.audit_service()
        assert isinstance(svc, AuditService)
        assert svc.repository is not None
        assert isinstance(svc.repository, DjangoORMAuditRepository)

"""Tests for audit DI container wiring."""

from __future__ import annotations

import di_core.containers
from audit.repositories import DjangoORMAuditRepository, InMemoryAuditRepository
from audit.services import MAIN_REPOSITORY_ALIAS, AuditService
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


class TestAuditAdditionalRepositoriesWiring:
    """Tests for the audit_additional_repositories provider."""

    def test_resolves_to_an_empty_mapping_by_default(self):
        """No replicas are configured for this project — the ORM log is the only store."""
        container = AppContainer()
        assert container.audit_additional_repositories() == {}

    def test_audit_service_gets_no_additional_repositories_by_default(self):
        """The service is usable with only a main repository."""
        svc = di_core.containers.container.audit_service()
        assert svc.additional_repositories == {}
        assert svc.repository_aliases == (MAIN_REPOSITORY_ALIAS,)

    def test_overriding_the_provider_reaches_the_service(self):
        """Registering a replica is a container edit, not a code change.

        Everything downstream -- the replication inside `persist`, the
        `repository=` argument on every read, `sync_repository` -- keys off what
        this provider returns.
        """
        container = di_core.containers.container
        replica = InMemoryAuditRepository()
        with container.audit_additional_repositories.override({"warehouse": replica}):
            svc = container.audit_service()

        assert svc.additional_repositories == {"warehouse": replica}
        assert svc.get_repository("warehouse") is replica
        assert isinstance(svc.get_repository(), DjangoORMAuditRepository)

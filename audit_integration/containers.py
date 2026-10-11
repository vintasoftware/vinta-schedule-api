from dependency_injector import providers

from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import OrganizationAuditService
from di_core.base import BaseContainer


class AuditContainer(BaseContainer):
    """Providers for the audit trail."""

    #: The audit log's system of record. Every audit record is written here
    #: first, and this is what `AuditService` reads from unless a caller names
    #: another repository.
    audit_repository = providers.Singleton(OrganizationAuditRepository)

    #: Extra audit backends, keyed by the alias callers pass as
    #: `repository="..."` to the `AuditService` read methods and as the target
    #: of `sync_repository`. Empty by default: the ORM repository is the only
    #: one this project runs. Add an entry (a search index, a warehouse loader,
    #: an archive) and every record starts being replicated there, best effort,
    #: on top of the main write -- with `vinta_audit_logs.tasks.sync_audit_repository`
    #: available to backfill whatever replication missed.
    #:
    #: Do NOT register "main" here; the key belongs to `audit_repository` and
    #: `AuditService` drops it.
    audit_additional_repositories = providers.Dict({})

    #: `AuditService` takes its repositories as constructor arguments rather
    #: than resolving them through `@inject`: `vinta_audit_logs` is meant to be
    #: installed by projects that may not use dependency_injector at all, so the
    #: wiring lives here instead of in the package.
    audit_service = providers.Factory(
        OrganizationAuditService,
        repository=audit_repository,
        additional_repositories=audit_additional_repositories,
    )

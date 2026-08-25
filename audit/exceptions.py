"""Exceptions raised by the audit app."""


class AuditError(Exception):
    """Base class for every error the audit app raises."""


class UnknownAuditRepositoryError(AuditError):
    """A caller named an audit repository the service does not have.

    Raised by ``AuditService.get_repository`` (and therefore by every read and
    sync method that takes a repository alias). Naming a repository that is not
    configured is a programming or configuration error, not a runtime condition
    to absorb: silently falling back to the main repository would answer a query
    about one store with data from another.
    """

    def __init__(self, alias: str, available: tuple[str, ...]) -> None:
        self.alias = alias
        self.available = available
        super().__init__(
            f"Unknown audit repository {alias!r}. Available: {', '.join(available) or '(none)'}."
        )

from __future__ import annotations

import abc

from audit.types import AuditPage, AuditQuery, AuditRecord, AuditRecordData


class AuditRepository(abc.ABC):
    """Backend-agnostic interface for audit record storage.

    Read + append only. No update, no delete.
    """

    @abc.abstractmethod
    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record.

        Args:
            data: The record data to persist.

        Returns:
            The persisted AuditRecord with id and created_at populated.
        """
        ...

    @abc.abstractmethod
    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by id.

        Args:
            audit_id: The audit record id.

        Returns:
            The AuditRecord if found, None otherwise.
        """
        ...

    @abc.abstractmethod
    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str = "-created_at",
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Args:
            q: The query filter/search object.
            offset: Number of records to skip (default 0).
            limit: Maximum records to return (default 50).
            ordering: Field(s) to order by, with optional - prefix for descending
                (default "-created_at").

        Returns:
            AuditPage containing items and total count.
        """
        ...

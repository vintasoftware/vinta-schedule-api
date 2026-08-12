"""Shared queryset plumbing for organization-scoped models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from vinta_orgs.querysets import SingleOrganizationQuerySet

from common.exceptions import OrganizationCannotBeUpdatedError


if TYPE_CHECKING:
    from organizations.models import Organization


class OrganizationScopedQuerySet(SingleOrganizationQuerySet):
    """``SingleOrganizationQuerySet`` that also accepts an organization *id*.

    The package types ``filter_by_organization`` / ``exclude_by_organization`` as
    taking an ``Organization``. Django resolves a related-field lookup given a
    primary key just as happily, and this codebase overwhelmingly holds the id
    rather than the instance (``membership.organization_id``,
    ``context.organization.id``, a task argument) -- fetching the row only to
    filter by it would be a query bought for nothing.

    Only the annotation is widened; the bodies are the package's. One behaviour
    is *added* -- see :meth:`update`.
    """

    #: Both spellings of the column a bulk ``UPDATE`` must never write.
    _ORGANIZATION_UPDATE_KWARGS = ("organization", "organization_id")

    def filter_by_organization(self, organization: Organization | int) -> Self:  # type: ignore[override]
        return super().filter_by_organization(organization)  # type: ignore[arg-type]

    def exclude_by_organization(self, organization: Organization | int) -> Self:  # type: ignore[override]
        return super().exclude_by_organization(organization)  # type: ignore[arg-type]

    def update(self, **kwargs: Any) -> int:
        """Refuse to move rows between organizations; otherwise the package's.

        ``BaseOrganizationModelQuerySet.update`` (retired in this phase) raised
        on ``update(organization=...)`` / ``update(organization_id=...)``, and
        nothing in the package replaces it: its ``update()`` only takes care
        *not to write* ``organization`` while rewriting a safe relation's kwargs
        onto the concrete field, which says nothing about a caller naming the
        column outright. Without this,
        ``Calendar.objects.filter_by_organization(a).update(organization_id=b)``
        relocates rows across the tenant boundary and reports success.

        Refused rather than scoped: a bulk ``UPDATE`` has no instance to take a
        consistent organization from, so there is no correct value to write --
        see :class:`common.exceptions.OrganizationCannotBeUpdatedError`.
        """
        for name in self._ORGANIZATION_UPDATE_KWARGS:
            if name in kwargs:
                raise OrganizationCannotBeUpdatedError()
        return super().update(**kwargs)

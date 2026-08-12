"""Shared queryset plumbing for organization-scoped models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from vinta_orgs.querysets import SingleOrganizationQuerySet


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

    Only the annotation is widened; the bodies are the package's.
    """

    def filter_by_organization(self, organization: Organization | int) -> Self:  # type: ignore[override]
        return super().filter_by_organization(organization)  # type: ignore[arg-type]

    def exclude_by_organization(self, organization: Organization | int) -> Self:  # type: ignore[override]
        return super().exclude_by_organization(organization)  # type: ignore[arg-type]

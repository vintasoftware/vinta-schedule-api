"""This project's specialization of ``vinta_orgs.services``.

Package ``0.4.0`` deleted ``vinta_orgs.helpers`` -- the module of free functions
that created organizations and memberships and resolved a user's membership --
and replaced it with two generics a project binds to its swapped models once:
``OrganizationService[Organization]`` and
``MembershipService[Organization, OrganizationMembership]``. This module is that
binding, and it sits beside :mod:`common.organization_context` for the same
reason that module exists: **one project-owned module per package concern, and
call sites import ours.** Three package bumps in a row have shown that the cost
of an upstream break is set by how concentrated the coupling is.

``MembershipService`` derives the organization model from its membership model's
``organization`` foreign key and checks it against ``ORGANIZATION_MODEL``, so it
takes no organization-service argument -- the two cannot drift apart at runtime.

**These are not DI services.** They are the package's stateless, model-bound
operation objects, constructed once at import and deliberately *not* registered
in ``di_core.containers``: they carry no collaborators to inject and nothing
about them varies per environment. This project's own
``organizations.services.OrganizationService`` is an unrelated *domain* service
(invitations, seats, provisioning, notifications) that does go through DI; the
name collision is why the package class is imported under an alias below.
"""

from __future__ import annotations

from vinta_orgs.services import MembershipService
from vinta_orgs.services import OrganizationService as PackageOrganizationService

from organizations.models import Organization, OrganizationMembership


class Organizations(PackageOrganizationService[Organization]):
    """Organization operations, bound to ``organizations.Organization``."""

    model_class = Organization


class Memberships(MembershipService[Organization, OrganizationMembership]):
    """Membership operations, bound to ``organizations.OrganizationMembership``.

    ``resolve_for_user`` / ``resolve_organization_for_user`` are the package's
    membership-resolution table: no selection resolves a sole membership and
    raises ``AmbiguousOrganizationError`` on more than one, and a selection the
    user cannot reach raises ``OrganizationAccessDeniedError``.
    """

    model_class = OrganizationMembership


#: The single organization-operations object for this process.
organizations = Organizations()

#: The single membership-operations object for this process.
memberships = Memberships()


__all__ = [
    "Memberships",
    "Organizations",
    "memberships",
    "organizations",
]

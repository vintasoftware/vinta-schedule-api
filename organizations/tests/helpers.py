"""The sanctioned way for a test to build an ``OrganizationMembership``.

**Use this instead of ``baker.make(OrganizationMembership, ...)``** anywhere the
membership is meant to carry a capability -- which, since Phase 4 of the
vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``), is
what every permission class reads.

**Why.** Authorization used to read ``membership.role`` / ``is_billing_owner``
directly, so ``baker.make(OrganizationMembership, role=ADMIN)`` produced a
membership that every permission class treated as an admin. A permission class
now reads an organization-named permission check
(``organizations.authorization.has_organization_permission``, not
``user.has_perm``), which resolves through ``OrganizationMembership.groups`` --
and baker assigns no groups. Four ``membership.is_admin`` readers survive
outside the permission classes until Phase 6 drops the columns (enumerated in
``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``), so a raw ``baker.make``
is *also* the shape where those and a permission class disagree. Production
keeps the two representations in step through
``organizations.services.sync_membership_groups_from_role`` (the Phase 3
dual-write, deleted in Phase 6 along with the columns); a raw ``baker.make``
bypasses it and produces a membership shape that **cannot exist in production**:
privileged by role, unprivileged by permission.

The failure mode is not a red test. A test whose admin membership carries no
groups sees a *denial*, and a test that asserted a denial for some other reason
-- a missing entitlement, a wrong organization, a non-owned calendar -- keeps
passing while proving nothing. That is why the sweep to this helper had to be
exhaustive, and why ``manage.py check_privileged_membership_fixtures`` re-derives
the answer from the source tree on every run -- it scans the repo's test modules
and fails on any raw privileged ``baker.make`` that comes back. It runs as a
pre-commit hook and as its own CI step rather than as a test, because it walks a
few hundred modules and the suite's per-test budget is 10 seconds.

A ``post_save`` signal would have covered every write path including baker's,
and was deliberately rejected (**Decisions taken 2026-08-13** in the tracking
file): it is a production behaviour change made to serve tests, and Phase 6
would have had to unpick it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from model_bakery import baker

from organizations.models import OrganizationMembership, OrganizationRole
from organizations.services import sync_membership_groups_from_role


if TYPE_CHECKING:
    from organizations.models import Organization
    from users.models import User


def make_membership(**kwargs: Any) -> OrganizationMembership:
    """``baker.make(OrganizationMembership, **kwargs)`` plus the group sync.

    Takes exactly what ``baker.make`` took, so converting a call site is
    deleting the model argument. ``_quantity`` is deliberately unsupported --
    every privileged membership should be nameable in the test that builds it.
    """
    membership = baker.make(OrganizationMembership, **kwargs)
    sync_membership_groups_from_role(membership)
    return membership


def make_admin_membership(
    *, user: User, organization: Organization, **kwargs: Any
) -> OrganizationMembership:
    """A membership that may administer ``organization``.

    The spelling to prefer in new tests: it says what the membership *is for*
    rather than which column happens to encode it today, so Phase 6's column
    drop is a change to this function and to nothing that calls it.
    """
    kwargs.setdefault("role", OrganizationRole.ADMIN)
    return make_membership(user=user, organization=organization, **kwargs)


def make_billing_owner_membership(
    *, user: User, organization: Organization, **kwargs: Any
) -> OrganizationMembership:
    """A membership that may manage ``organization``'s billing without administering it."""
    kwargs.setdefault("role", OrganizationRole.MEMBER)
    kwargs.setdefault("is_billing_owner", True)
    return make_membership(user=user, organization=organization, **kwargs)


def grant_membership_groups(membership: OrganizationMembership) -> OrganizationMembership:
    """Bring an already-built membership's groups in step with its role.

    For the call sites that cannot use ``make_membership`` -- a membership built
    by a service under test, or one whose ``role`` a test mutates after
    creation, which is what every live role-changing path does through the same
    shim.
    """
    sync_membership_groups_from_role(membership)
    return membership

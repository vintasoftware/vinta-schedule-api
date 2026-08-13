"""The sanctioned way for a test to build an ``OrganizationMembership``.

**Use this instead of ``baker.make(OrganizationMembership, ...)``** anywhere the
membership is meant to carry a capability -- which, since Phase 4 of the
vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``), is
what every permission class reads.

**Why.** Authorization reads an organization-named permission check
(``organizations.authorization.has_organization_permission``, not
``user.has_perm``), which resolves through ``OrganizationMembership.groups`` --
and ``baker.make`` assigns no groups. In production every membership write goes
through ``organizations.services.assign_membership_groups``; a raw
``baker.make`` produces a membership in no group at all -- a legitimate shape
(an unprivileged member is in ``organization_member``, which carries nothing
either), but not the privileged one a test asking about admin behaviour needs.

The failure mode is not a red test. A test whose admin membership carries no
groups sees a *denial*, and a test that asserted a denial for some other reason
-- a missing entitlement, a wrong organization, a non-owned calendar -- keeps
passing while proving nothing. That is why the sweep to this helper had to be
exhaustive.

Since Phase 6 dropped the two flat capability columns there is no second
representation left to disagree with the groups: a test cannot produce a
membership that looks privileged to one reader and unprivileged to another, and
a test that wanted an admin and built it with ``baker.make`` sees the denial
directly.

A ``post_save`` signal would have covered every write path including baker's,
and was deliberately rejected (**Decisions taken 2026-08-13** in the tracking
file): it is a production behaviour change made to serve tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from model_bakery import baker

from organizations.models import OrganizationMembership
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
)
from organizations.services import assign_membership_groups


if TYPE_CHECKING:
    from collections.abc import Iterable

    from organizations.models import Organization
    from users.models import User


def make_membership(
    *, groups: Iterable[str] = (GROUP_ORGANIZATION_MEMBER,), **kwargs: Any
) -> OrganizationMembership:
    """``baker.make(OrganizationMembership, **kwargs)`` plus its groups.

    Takes exactly what ``baker.make`` took, so converting a call site is
    deleting the model argument. ``_quantity`` is deliberately unsupported --
    every privileged membership should be nameable in the test that builds it.

    ``groups`` defaults to ``organization_member``, matching what every
    production write path stores for a member with no capabilities.
    """
    membership = baker.make(OrganizationMembership, **kwargs)
    assign_membership_groups(membership, groups)
    return membership


def make_admin_membership(
    *, user: User, organization: Organization, **kwargs: Any
) -> OrganizationMembership:
    """A membership that may administer ``organization``."""
    kwargs.setdefault("groups", (GROUP_ORGANIZATION_ADMIN,))
    return make_membership(user=user, organization=organization, **kwargs)


def make_billing_owner_membership(
    *, user: User, organization: Organization, **kwargs: Any
) -> OrganizationMembership:
    """A membership that may manage ``organization``'s billing without administering it."""
    kwargs.setdefault("groups", (GROUP_ORGANIZATION_BILLING_OWNER,))
    return make_membership(user=user, organization=organization, **kwargs)


def grant_membership_groups(
    membership: OrganizationMembership,
    groups: Iterable[str],
) -> OrganizationMembership:
    """Put an already-built membership in ``groups``.

    For the call sites that cannot use ``make_membership`` -- a membership built
    by a service under test, or one whose capabilities a test changes after
    creation, which is what the live re-grouping path does through the same
    writer.

    ``groups`` is **required and has no default**, unlike ``make_membership``'s.
    That asymmetry is deliberate. This function used to read a membership's
    ``role`` column and derive the groups from it, so the capability was named
    at the *creation* call it wrapped; when Phase 6 dropped the column, giving
    the parameter a default silently moved that decision here. Whichever default
    were chosen would be wrong for some caller and wrong *silently*, because no
    assertion reads the groups directly -- exactly how
    ``organizations/tests/test_org_resolution.py``'s ten member fixtures became
    ten admins under a ``(GROUP_ORGANIZATION_ADMIN,)`` default without a single
    test going red. A caller that has to type the capability out cannot get one
    it did not ask for.
    """
    assign_membership_groups(membership, groups)
    return membership

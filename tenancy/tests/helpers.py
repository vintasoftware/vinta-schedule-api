"""Shared helpers for the ``tenancy`` test suite."""

from tenancy.models import Organization


def clear_organization_slug(organization: Organization) -> Organization:
    """Force ``organization`` into the "no public identifier picked yet" state.

    Before Phase 1c of the vinta-django-orgs migration that state was simply
    ``slug = NULL``, and any organization created without a slug was in it. It is
    no longer reachable through a supported path: ``Organization.slug`` is
    inherited from ``AbstractOrganization``, which declares it NOT NULL and
    unique, and ``Organization.save()`` derives a slug for any row created
    without one.

    The branding gates still branch on it -- ``BrandingWriteGateReason.NO_SLUG``,
    and the deliberate *absence* of the slug condition from
    ``is_branding_eligible_organization`` -- and Phase 1c changes no
    authorization logic, so that branch and its coverage stay. This helper is how
    the tests reach it: ``queryset.update()`` writes past ``save()``'s derivation,
    and ``""`` is what the gates' ``if not organization.slug`` test reads as
    "unset" now that ``None`` cannot be stored.

    Note the constraint this leaves in place: ``slug`` is unique, so **at most one
    organization per test** may be put in this state.

    Args:
        organization: The organization to blank. Mutated in place and returned
            for convenience.

    Returns:
        The same instance, refreshed from the database.
    """
    Organization.objects.filter(pk=organization.pk).update(slug="")
    organization.refresh_from_db()
    return organization

"""Per-organization feature flags, stored in ``OrganizationFeatureFlag`` rows.

A missing row means the flag is off. Ops toggle rows from Django admin.
"""

from organizations.models import OrganizationFeatureFlag


RESOURCE_CALENDAR_PROVIDER_SYNC = "resource_calendar_provider_sync"


def is_enabled(key: str, organization_id: int) -> bool:
    """Return whether ``key`` is switched on for the given organization."""
    return (
        OrganizationFeatureFlag.objects.filter_by_organization(organization_id)
        .filter(key=key, enabled=True)
        .exists()
    )


def organization_ids_with_flag(key: str) -> list[int]:
    """Return the ids of every organization that has ``key`` switched on."""
    # Cross-organization on purpose: beat tasks iterate every flag-on organization and
    # run with no organization bound.
    return list(
        OrganizationFeatureFlag.objects.unscoped()
        .filter(key=key, enabled=True)
        .order_by("organization_id")
        .values_list("organization_id", flat=True)
    )

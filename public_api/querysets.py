from organizations.querysets import BaseOrganizationModelQuerySet


class SystemUserQuerySet(BaseOrganizationModelQuerySet):
    """QuerySet for SystemUser with domain-specific filtering methods."""

    def live(self) -> "SystemUserQuerySet":
        """System users that still exist and can still authenticate.

        ``SystemUser`` carries two independent off-switches — ``is_active=False``
        (revoked but retained) and ``deleted_at`` (soft delete) — and a row in
        either state consumes nothing. The definition belongs next to the model
        that owns both columns rather than in each consumer.
        """
        return self.filter(is_active=True, deleted_at__isnull=True)

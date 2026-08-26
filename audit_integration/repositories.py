"""The ORM repository, taught this project's scope and identity columns.

``DjangoORMAuditRepository`` writes the columns ``vinta_audit_logs`` defines.
This subclass fills the ones :mod:`audit_integration.models` adds -- the
organization behind a scope, the membership's groups and permissions and the
token scopes behind an identity -- and reads them back, so a record makes the
same round trip through this repository that it makes through any other.

It also teaches the read side about :class:`audit_integration.types.OrganizationAuditQuery`,
so a caller can filter on those same project-specific columns. Both halves live
here because they are the same seam seen from two sides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Q

from vinta_audit_logs.repositories import DjangoORMAuditRepository
from vinta_audit_logs.types import AuditQuery, IdentitySnapshot, ScopeRef

from audit_integration.types import OrganizationAuditQuery


if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model


class OrganizationAuditRepository(DjangoORMAuditRepository):
    """Maps portable audit DTOs onto this project's scope and identity models."""

    # ------------------------------------------------------------------
    # Read: the portable filters, plus this project's own
    # ------------------------------------------------------------------

    def _filtered_queryset(self, q: AuditQuery):
        """Apply the portable filters, then this project's extensions.

        The base class handles every field of ``AuditQuery`` and produces a
        queryset with no joins -- each portable filter reads a denormalized
        column on the audit row itself. What is added below reaches columns the
        audit row does not carry, so each one joins.

        That is a deliberate trade, not an oversight. A filter on the actor's
        email cannot be answered from the audit row without copying every user's
        email onto every audit record and keeping the copies correct forever.
        Joining is the honest option; it is simply not the option to reach for on
        a page a user loads repeatedly. See ``audit_integration.types`` for when
        to use these.

        A plain ``AuditQuery`` takes the fast path unchanged, so nothing pays for
        this unless it asks to.
        """
        qs = super()._filtered_queryset(q)
        if not isinstance(q, OrganizationAuditQuery):
            return qs

        if q.organization_ids is not None:
            # The one project filter that does not join: the scope key IS the
            # organization pk, so this lands on the same index every browse uses.
            qs = qs.filter(scope_key__in=[str(pk) for pk in q.organization_ids])

        if q.actor_user_emails is not None:
            # audit -> identity -> user. Case-insensitive because email is, and
            # an OR of iexact rather than __in so the comparison stays
            # case-insensitive per value.
            qs = qs.filter(
                self._or_all(Q(actor__user__email__iexact=email) for email in q.actor_user_emails)
            )

        if q.actor_group_ids is not None:
            qs = qs.filter(actor__membership_groups__id__in=q.actor_group_ids).distinct()

        if q.actor_permission_codenames is not None:
            qs = qs.filter(
                actor__membership_permissions__codename__in=q.actor_permission_codenames
            ).distinct()

        return qs

    @staticmethod
    def _or_all(conditions) -> Q:
        """OR a series of conditions, matching nothing when there are none.

        Same empty-set rule the portable filters follow: an empty list is an
        active filter that nothing satisfies, not an absent one.
        """
        combined: Q | None = None
        for condition in conditions:
            combined = condition if combined is None else combined | condition
        return combined if combined is not None else Q(pk__in=[])

    def build_scope_defaults(self, ref: ScopeRef) -> dict[str, Any]:
        """Fill ``OrganizationAuditScope.organization`` when a scope is created.

        ``scope_key`` is the organization's pk as a string (see
        ``OrganizationAuditScope.build_scope_key``), so the reverse mapping is a
        parse.

        A key that does not parse raises rather than quietly becoming the global
        scope. Silently reassigning a record to a different scope than the one it
        was emitted for is worse than losing the write: the record still looks
        real, is still returned by queries, and is attributed to the wrong place.
        The only way to reach this is a corrupt payload or a record replicated
        from a store using a different key scheme -- both worth hearing about.
        """
        organization_id: int | None = None
        if ref.scope_key:
            try:
                organization_id = int(ref.scope_key)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Audit scope key {ref.scope_key!r} is not an organization id. "
                    f"This project keys scopes on Organization.pk; a record carrying "
                    f"anything else cannot be scoped correctly."
                ) from exc
        return {"organization_id": organization_id, "label": ref.label}

    def build_identity_defaults(self, snapshot: IdentitySnapshot) -> dict[str, Any]:
        """Add this project's identity columns to the generic ones.

        The same values stay in ``metadata`` as well as landing in their own
        columns. That redundancy is the point: ``metadata`` is what a replica with
        the stock identity model receives, so a record replicated to a warehouse
        still carries the token scopes even though that store has no column for
        them.

        The membership's groups and permissions are not here -- they are
        relations, which ``bulk_create`` cannot populate. See
        :meth:`attach_identity_relations`.
        """
        defaults = super().build_identity_defaults(snapshot)
        metadata = snapshot.metadata or {}
        defaults["system_user_scopes"] = list(metadata.get("system_user_scopes") or [])
        defaults["system_user_scoped_to_membership"] = metadata.get(
            "system_user_scoped_to_membership"
        )
        return defaults

    def attach_identity_relations(
        self,
        identities: Sequence[Model],
        snapshots: Sequence[IdentitySnapshot],
    ) -> None:
        """Link each identity to the groups and permissions its membership held.

        Two statements for the whole batch, whatever its size: the link rows are
        built for every identity at once and handed to one ``bulk_create`` each.
        Assigning through ``identity.membership_groups.set(...)`` per row would be
        two queries per audit record instead.

        ``ignore_conflicts`` because re-persisting a record (a retried task, a
        re-run backfill) re-creates its identity rows, and a link that is already
        there needs nothing done to it.

        The group and permission *ids* come from ``metadata``; the durable
        ``group_names`` / ``permission_keys`` snapshot is written by
        ``build_identity_defaults`` above and is what survives those rows being
        deleted.
        """
        group_links = []
        permission_links = []
        for identity, snapshot in zip(identities, snapshots, strict=True):
            metadata = snapshot.metadata or {}
            group_links.extend(
                (identity.pk, group_id) for group_id in metadata.get("membership_group_ids") or []
            )
            permission_links.extend(
                (identity.pk, permission_id)
                for permission_id in metadata.get("membership_permission_ids") or []
            )

        identity_model = type(identities[0]) if identities else None
        if identity_model is None:
            return

        if group_links:
            through = identity_model.membership_groups.through
            through.objects.bulk_create(
                [
                    through(organizationauditidentity_id=identity_id, group_id=group_id)
                    for identity_id, group_id in group_links
                ],
                ignore_conflicts=True,
            )
        if permission_links:
            through = identity_model.membership_permissions.through
            through.objects.bulk_create(
                [
                    through(organizationauditidentity_id=identity_id, permission_id=permission_id)
                    for identity_id, permission_id in permission_links
                ],
                ignore_conflicts=True,
            )

    def identity_to_snapshot(self, identity: Model) -> IdentitySnapshot:
        """Read the project columns back into ``metadata``.

        The inverse of :meth:`build_identity_defaults`. Reading from the columns
        rather than from the stored ``metadata`` blob means the DTO reflects what
        the database actually holds, which is what a sync out of this repository
        should carry.

        The group and permission *ids* are deliberately not read back. They mean
        nothing outside this database, so a record synced elsewhere would carry
        numbers that resolve to different rows -- or to nothing. The names and
        keys travel instead, on the inherited snapshot fields.
        """
        snapshot = super().identity_to_snapshot(identity)
        metadata = dict(snapshot.metadata)
        metadata.pop("membership_group_ids", None)
        metadata.pop("membership_permission_ids", None)
        if identity.system_user_scopes:
            metadata["system_user_scopes"] = list(identity.system_user_scopes)
        if identity.system_user_scoped_to_membership is not None:
            metadata["system_user_scoped_to_membership"] = identity.system_user_scoped_to_membership
        return IdentitySnapshot(
            identity_type=snapshot.identity_type,
            identity_key=snapshot.identity_key,
            identity_label=snapshot.identity_label,
            user_id=snapshot.user_id,
            is_staff=snapshot.is_staff,
            is_superuser=snapshot.is_superuser,
            group_names=snapshot.group_names,
            permission_keys=snapshot.permission_keys,
            metadata=metadata,
        )

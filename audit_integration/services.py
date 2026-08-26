"""This project's audit service: the actor and scope builders its call sites use.

``vinta_audit_logs.AuditService`` knows how to write a record. It does not know
that this project has organizations, memberships, API tokens and single-use
calendar codes. Everything below is that knowledge, and nothing below is about
storage -- the writing half of the same seam is
:class:`audit_integration.repositories.OrganizationAuditRepository`.

Every builder here runs **synchronously**, in the request, and returns a frozen
snapshot. That is not incidental: a membership's role, a token's scopes and a
user's groups are all mutable state, and an audit trail that re-reads them in
the worker records what was true at write time rather than at action time.
"""

from typing import Annotated, Any

from dependency_injector.wiring import Provide, inject
from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.services import AuditService
from vinta_audit_logs.types import IdentitySnapshot, ScopeRef

from audit_integration.constants import AuditActorType
from audit_integration.repositories import OrganizationAuditRepository


class OrganizationAuditService(AuditService):
    """AuditService with this project's notions of scope and actor."""

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def scope_from(self, obj: Any) -> ScopeRef:
        """Build the scope for an organization, an organization id, or None.

        Accepts an id as well as an instance because most call sites already
        hold ``organization_id`` and making them fetch the row to record an
        audit entry would be a query per action for nothing.
        """
        if obj is None:
            return ScopeRef.global_scope()
        organization_id = getattr(obj, "pk", obj)
        return ScopeRef(
            scope_type=ScopeType.SCOPED,
            scope_key=str(organization_id),
            label=self._organization_label(obj),
        )

    def scope_from_organization_id(self, organization_id: int | None) -> ScopeRef:
        """Build the scope for an organization id. None means the global scope."""
        return self.scope_from(organization_id)

    @staticmethod
    def _organization_label(obj: Any) -> str:
        """A cheap display name for a scope, when the caller passed a row.

        Empty when the caller passed a bare id -- the label is only used the
        first time a scope row is created, and paying a query for it on every
        record would be a poor trade.
        """
        name = getattr(obj, "name", None)
        return str(name) if name else ""

    # ------------------------------------------------------------------
    # Actors
    # ------------------------------------------------------------------

    def actor_from_membership(self, membership: Any) -> IdentitySnapshot:
        """Snapshot an OrganizationMembership as the actor.

        Captures the groups and permissions the membership holds at call time, so
        the worker never re-reads a membership row that may have changed or been
        deleted. Not a derived role label: authorization here is groups and
        permissions, and the label would record strictly less than what the
        membership actually held.

        The identity key is the org-scoped ``user_id``, not the membership pk,
        per the OrganizationMembershipForeignKey convention: a membership is
        identified by ``(organization_id, user_id)``, and the organization is
        already on the record as its scope.
        """
        user = getattr(membership, "user", None)
        # Two queries, and two however many groups or permissions there are:
        # each ``values_list`` fetches the ids and the durable names together, so
        # neither the key-building below nor the relation-attaching in the
        # repository has to walk back to the database per row. Building the
        # permission key from ``content_type__app_label`` is what would otherwise
        # be an N+1 -- ``permission.content_type.app_label`` is a query each.
        groups = list(membership.groups.values_list("pk", "name"))
        permissions = list(
            membership.permissions.values_list("pk", "content_type__app_label", "codename")
        )
        return IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP,
            identity_key=str(membership.user_id),
            identity_label=self.label_for_user(user) if user is not None else "",
            user_id=membership.user_id,
            is_staff=bool(getattr(user, "is_staff", False)),
            is_superuser=bool(getattr(user, "is_superuser", False)),
            # The durable snapshot: names and keys, which outlive the rows they
            # were read from.
            group_names=sorted(name for _pk, name in groups),
            permission_keys=sorted(
                f"{app_label}.{codename}" if app_label else codename
                for _pk, app_label, codename in permissions
            ),
            # The ids the repository needs to attach the live relations. In
            # metadata rather than as snapshot fields because they are useless to
            # any other backend: an id means nothing outside this database.
            metadata={
                "membership_group_ids": [pk for pk, _name in groups],
                "membership_permission_ids": [pk for pk, _app, _code in permissions],
            },
        )

    def actor_from_system_user(self, system_user: Any) -> IdentitySnapshot:
        """Snapshot a SystemUser (an API token) as the actor.

        The scopes queryset is evaluated **now** so the snapshot is correct even
        if the token's ResourceAccess rows change before the write runs. That is
        the whole reason this is a snapshot and not a foreign key.
        """
        scopes = [resource.resource_name for resource in system_user.available_resources.all()]
        return IdentitySnapshot(
            identity_type=AuditActorType.SYSTEM_USER,
            identity_key=str(system_user.id),
            identity_label=str(getattr(system_user, "name", "") or f"system-user:{system_user.id}"),
            metadata={
                "system_user_scopes": scopes,
                "system_user_scoped_to_membership": system_user.scoped_to_membership_user_id,
            },
        )

    def actor_from_single_use_code(self, token: Any) -> IdentitySnapshot:
        """Snapshot a CalendarManagementToken (a single-use code) as the actor."""
        return IdentitySnapshot(
            identity_type=AuditActorType.SINGLE_USE_CODE,
            identity_key=str(token.id),
            identity_label=f"single-use-code:{token.id}",
        )

    def system_actor(self) -> IdentitySnapshot:
        """The actor for an action nothing and nobody was behind.

        Kept as its own name (rather than the base class's ``system_identity``)
        because it is what every call site in this project already says.
        """
        return IdentitySnapshot(
            identity_type=AuditActorType.SYSTEM,
            identity_key="",
            identity_label="system",
        )

    def actor_from_user(self, user: Any, organization_id: int) -> IdentitySnapshot:
        """Resolve a User acting inside an organization to an actor snapshot.

        Looks up the membership identifying this user in the organization and,
        when present, returns a membership actor capturing its role. Falls back
        to the system actor when the user has no membership there -- mirroring
        the orphan-ownership guard, so a non-member acting still produces a
        record with a stable actor rather than a dangling reference.
        """
        from organizations.models import OrganizationMembership

        membership = (
            OrganizationMembership.objects.filter(
                user_id=user.id,
                organization_id=organization_id,
            )
            .select_related("user")
            .first()
        )
        if membership is None:
            return self.system_actor()
        return self.actor_from_membership(membership)

    def actor_from_user_or_token(
        self,
        user_or_token: Any,
        organization_id: int,
        single_use_token: Any | None = None,
    ) -> IdentitySnapshot:
        """Resolve a calendar service ``user_or_token`` value to an actor.

        The calendar services carry a ``user_or_token`` of
        ``User | str | SystemUser | None`` on their auth context. This maps each
        variant to the right actor:

        - ``User``       -> membership actor (or system, via actor_from_user)
        - ``SystemUser`` -> system-user actor with scopes
        - ``str``        -> a single-use CalendarManagementToken *code*. When the
          resolved token row is supplied via ``single_use_token``, attribute the
          action to that token; otherwise fall back to system.
        - ``None``       -> system actor.
        """
        from public_api.models import SystemUser
        from users.models import User

        if isinstance(user_or_token, User):
            return self.actor_from_user(user_or_token, organization_id)
        if isinstance(user_or_token, SystemUser):
            return self.actor_from_system_user(user_or_token)
        if isinstance(user_or_token, str) and single_use_token is not None:
            return self.actor_from_single_use_code(single_use_token)
        return self.system_actor()

    def affected_from_membership_ids(
        self, organization_id: int | None, membership_user_ids: Any
    ) -> list[IdentitySnapshot]:
        """Snapshot the memberships an action affected, given their user ids.

        Builds the snapshots from the ids alone and runs **no queries**. That is
        the important property: this is called on the request path, inside the
        business transaction being audited, and instrumentation that adds a
        query per audited write is instrumentation that shows up in latency
        graphs and in query-count regressions.

        The trade is a thinner snapshot for affected parties than for the actor
        -- an id, no display name, no role. That matches what the log has always
        held for them, and it is enough for the question they answer: which
        people did this action touch. The actor, whose full context genuinely
        matters, is snapshotted properly by the builders above, from a row the
        caller already had in hand.

        Args:
            organization_id: The organization the memberships belong to. Not
                used to look anything up; it is already on the record as its
                scope, and a membership id is only meaningful within it.
            membership_user_ids: Org-scoped user ids, in any order, possibly
                with repeats.

        Returns:
            One snapshot per distinct id, in the order first seen.
        """
        return [
            IdentitySnapshot(
                identity_type=AuditActorType.MEMBERSHIP,
                identity_key=str(user_id),
                user_id=user_id,
            )
            for user_id in dict.fromkeys(membership_user_ids)
        ]


@inject
def audit_service_factory(
    audit_service: Annotated[OrganizationAuditService | None, Provide["audit_service"]] = None,
) -> OrganizationAuditService:
    """Build the service the background tasks write through.

    Named by ``AUDIT_SERVICE_FACTORY``. The indirection exists so
    ``vinta_audit_logs`` never has to import this project's DI container -- the
    package names a callable, and this project's callable happens to reach into
    the container.
    """
    if audit_service is None:
        raise RuntimeError(
            "DI container is not wired; audit_service_factory cannot resolve "
            "audit_service before di_core.apps.DICoreConfig.ready() runs."
        )
    return audit_service


@inject
def audit_repository_factory(
    audit_repository: Annotated[
        OrganizationAuditRepository | None, Provide["audit_repository"]
    ] = None,
) -> OrganizationAuditRepository:
    """Build the repository the audit admin reads through.

    Named by ``AUDIT_REPOSITORY_FACTORY``. Same indirection, same reason.
    """
    if audit_repository is None:
        raise RuntimeError(
            "DI container is not wired; audit_repository_factory cannot resolve "
            "audit_repository before di_core.apps.DICoreConfig.ready() runs."
        )
    return audit_repository

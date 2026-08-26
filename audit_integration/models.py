"""This project's concrete audit scope and identity.

``vinta_audit_logs`` does not know what a tenant or an actor is here; these two
models are the answer, and ``AUDIT_SCOPE_MODEL`` / ``AUDIT_IDENTITY_MODEL`` point
at them.

Neither carries ``SingleOrganizationModelMixin``, and that is deliberate. The
audit log is append-only and has to outlive the rows it describes, so an
implicitly tenant-scoped manager -- which raises when nothing is bound to the
context, and which staff requests never bind -- is the wrong shape for it.
Tenant isolation on reads is explicit instead: every query names the scope it
wants, through ``AuditQuery.scope_keys``, and ``OrganizationAuditScope.scope_key``
is the organization's primary key as a string.
"""

from typing import ClassVar

from django.db import models

from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.models import AbstractAuditIdentity, AbstractAuditScope

from organizations.models import Organization


# ``Organization`` is imported for real, not under TYPE_CHECKING: it is the type
# argument of the base class below, which is evaluated when the class is created.
class OrganizationAuditScope(AbstractAuditScope[Organization]):
    """An audit scope that is one organization, or the whole installation.

    ``organization`` is nullable so the global scope has a row too: a platform
    action belongs to no tenant but still needs somewhere to hang.

    ``PROTECT`` rather than ``CASCADE``: deleting an organization must not delete
    the record of what happened inside it. If an organization ever does need to
    go, its audit rows are the thing to deal with first, deliberately -- not
    something to lose as a side effect.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_scopes",
    )

    @property
    def scope(self):
        return self.organization

    @scope.setter
    def scope(self, value):
        self.organization = value
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self):
        self.organization = None
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        """The organization's pk as a string; "" for the global scope.

        A pk rather than a slug or a name: the key is what every audit row is
        indexed by, so it has to be immutable for the life of the scope, and a
        renameable field is not.
        """
        return "" if self.organization_id is None else str(self.organization_id)

    class Meta:
        constraints: ClassVar = [
            # The same invariant ``validate_scope`` checks, held where ``save``
            # cannot reach: ``bulk_create`` and ``QuerySet.update`` never call it.
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, organization__isnull=True)
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & models.Q(organization__isnull=False)
                ),
                name="audit_org_scope_type_and_org_agree",
            ),
            models.UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="audit_org_scope_unique_key_per_type",
            ),
        ]

    def __str__(self) -> str:
        return self.label or (f"Organization {self.scope_key}" if self.scope_key else "Global")


class OrganizationAuditIdentity(AbstractAuditIdentity):
    """Who acted, with the columns this project wants to query on.

    Everything here is also written into ``AbstractAuditIdentity.metadata``, so a
    repository that has never heard of this model still receives a complete
    snapshot. What earns a real column is what this project needs to *filter and
    join on*: what a membership could do at the time, and what an API token could
    reach.

    One row per audit record, per ``AbstractAuditIdentity``. The values are a
    snapshot of the moment, not a live join.

    Authorization is groups and permissions here -- there is no ``role`` column on
    ``OrganizationMembership`` any more, and ``membership_role_label()`` is a
    label derived from one permission rather than a fact. Storing the derived
    label would have recorded strictly less than what the membership actually
    held, and would have gone stale the moment the definition of "admin" moved.
    So the groups and permissions themselves are what get stored, twice over:

    * ``membership_groups`` / ``membership_permissions`` are live relations, for
      the queries that want to join -- "every action taken by anyone holding this
      permission".
    * ``group_names`` / ``permission_keys`` (inherited, JSON lists of strings) are
      the durable snapshot. Groups and permissions get renamed and deleted, and
      when they do the relations above lose their rows -- these do not. They are
      also what a replica with the stock identity model receives.

    JSON lists rather than the comma-separated text this pattern usually reaches
    for: same durability, but they survive a name containing a comma, and
    Postgres can index and search them with ``?`` / ``@>`` where CSV forces a
    ``LIKE '%,name,%'`` over the whole column.
    """

    # The groups and permissions the *membership* carried at emit time -- the
    # organization-scoped ones from OrganizationMembership, not the user's global
    # Django groups. Live relations: a deleted group takes its link rows with it,
    # which is exactly why the inherited name snapshots exist alongside.
    membership_groups = models.ManyToManyField("auth.Group", blank=True, related_name="+")
    membership_permissions = models.ManyToManyField("auth.Permission", blank=True, related_name="+")

    # The resource names an API token could reach when it acted. A JSON list,
    # matching the shape ``SystemUser.available_resources`` produces.
    system_user_scopes = models.JSONField(default=list, blank=True)

    # The membership an API token was restricted to, as the org-scoped user_id
    # (the OrganizationMembershipForeignKey convention: a membership is
    # identified by (organization_id, user_id)). Null when the token is
    # organization-wide.
    system_user_scoped_to_membership = models.BigIntegerField(null=True, blank=True)

    class Meta(AbstractAuditIdentity.Meta):
        abstract = False
        swappable = "AUDIT_IDENTITY_MODEL"

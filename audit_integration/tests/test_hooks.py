"""The seam between ``vinta_audit_logs`` and this project.

Two halves, tested separately because they run in different places:

* The service builders run **synchronously**, in the request, and turn this
  project's principals into portable snapshots.
* The repository hooks run in the worker, and turn those snapshots into rows of
  this project's concrete scope and identity models.
"""

from django.contrib.auth.models import Group, Permission

import pytest
from model_bakery import baker
from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.types import IdentitySnapshot, ScopeRef

from audit_integration.constants import AuditActorType
from audit_integration.models import OrganizationAuditIdentity, OrganizationAuditScope
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import OrganizationAuditService
from organizations.models import Organization, OrganizationMembership
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Hooks Test Org")


@pytest.fixture
def service() -> OrganizationAuditService:
    return OrganizationAuditService(repository=OrganizationAuditRepository())


# ---------------------------------------------------------------------------
# Service hooks: principals -> portable snapshots, synchronously
# ---------------------------------------------------------------------------


def test_scope_from_organization_id_keys_on_the_pk(service, organization):
    """The scope key is the pk, so it cannot change under records already written."""
    ref = service.scope_from_organization_id(organization.id)

    assert ref.scope_type == ScopeType.SCOPED
    assert ref.scope_key == str(organization.id)


def test_scope_from_none_is_the_global_scope(service):
    """A platform action belongs to no tenant but still needs a scope."""
    ref = service.scope_from_organization_id(None)

    assert ref.scope_type == ScopeType.GLOBAL
    assert ref.scope_key == ""


def test_system_actor_carries_no_user(service):
    """A cron job is an actor, and it is not a person."""
    actor = service.system_actor()

    assert actor.identity_type == AuditActorType.SYSTEM
    assert actor.identity_key == ""
    assert actor.user_id is None


def test_actor_from_user_without_a_membership_falls_back_to_system(service, organization):
    """A non-member acting leaves a record with a stable actor, not a dangling one."""
    user = baker.make(User)

    actor = service.actor_from_user(user, organization.id)

    assert actor.identity_type == AuditActorType.SYSTEM


def test_affected_from_membership_ids_runs_no_queries(
    service, organization, django_assert_num_queries
):
    """Instrumentation must not add queries to the business path it audits.

    This runs inside the transaction of the write being audited, so a query per
    audited action would show up in latency and in every query-count assertion
    in the project.
    """
    with django_assert_num_queries(0):
        affected = service.affected_from_membership_ids(organization.id, [4, 5, 4])

    assert [snapshot.identity_key for snapshot in affected] == ["4", "5"]
    assert all(snapshot.identity_type == AuditActorType.MEMBERSHIP for snapshot in affected)


def test_label_for_user_never_raises(service):
    """A display name is a nicety; failing to read one must not fail the action.

    ``get_full_name`` on this project's user reaches through to a profile row and
    raises when there is none -- which is exactly why the label builder reads only
    the username, and swallows even that.
    """

    class Hostile:
        pk = 1

        def get_username(self):
            raise RuntimeError("no username for you")

    assert service.label_for_user(Hostile()) == ""


# ---------------------------------------------------------------------------
# Repository hooks: portable snapshots -> this project's rows
# ---------------------------------------------------------------------------


def test_scope_rows_get_their_organization(organization):
    """The scope key parses back into the FK this project's scope model carries."""
    repository = OrganizationAuditRepository()

    scope = repository.resolve_scope(
        ScopeRef(
            scope_type=ScopeType.SCOPED, scope_key=str(organization.id), label="Hooks Test Org"
        )
    )

    assert isinstance(scope, OrganizationAuditScope)
    assert scope.organization_id == organization.id
    assert scope.scope_key == str(organization.id)


def test_a_malformed_scope_key_is_refused(organization):
    """A key that is not an organization id fails loudly.

    Quietly filing the record under the global scope would leave a real-looking
    record attributed to the wrong place, which is worse than losing the write.
    """
    repository = OrganizationAuditRepository()

    with pytest.raises(ValueError, match="not an organization id"):
        repository.resolve_scope(ScopeRef(scope_type=ScopeType.SCOPED, scope_key="not-an-id"))


def test_identity_metadata_lands_in_project_columns():
    """The role and token scopes get real columns, so they can be queried on."""
    repository = OrganizationAuditRepository()

    identity = repository.resolve_identity(
        IdentitySnapshot(
            identity_type=AuditActorType.SYSTEM_USER,
            identity_key="42",
            metadata={
                "system_user_scopes": ["calendars"],
                "system_user_scoped_to_membership": 9,
            },
        )
    )

    assert isinstance(identity, OrganizationAuditIdentity)
    assert identity.system_user_scopes == ["calendars"]
    assert identity.system_user_scoped_to_membership == 9


def test_identity_metadata_is_kept_as_well_as_split_out():
    """``metadata`` survives alongside the columns, for replicas without them.

    A record replicated into a store using the stock identity model still has to
    carry the token's scopes, and ``metadata`` is where it carries them.
    """
    repository = OrganizationAuditRepository()

    identity = repository.resolve_identity(
        IdentitySnapshot(
            identity_type=AuditActorType.SYSTEM_USER,
            identity_key="3",
            metadata={"system_user_scopes": ["calendars"]},
        )
    )
    snapshot = repository.identity_to_snapshot(identity)

    assert identity.system_user_scopes == ["calendars"]
    assert snapshot.metadata["system_user_scopes"] == ["calendars"]


def test_a_membership_actor_records_groups_and_permissions(organization, service):
    """What a membership could do at the time, both durably and joinably.

    The old schema recorded a derived ``role`` label. There is no role column any
    more -- authorization is groups and permissions -- so what gets recorded is
    the groups and permissions themselves.
    """
    group, _ = Group.objects.get_or_create(name="organization_admin")
    permission = Permission.objects.filter(codename__isnull=False).first()
    user = baker.make(User)
    membership = OrganizationMembership.objects.create(organization=organization, user=user)
    membership.groups.add(group)
    membership.permissions.add(permission)

    snapshot = service.actor_from_membership(membership)

    # Durable: names and keys, which survive the group being renamed or deleted.
    assert snapshot.group_names == ["organization_admin"]
    assert snapshot.permission_keys == [
        f"{permission.content_type.app_label}.{permission.codename}"
    ]
    # Joinable: the ids the repository turns into relations.
    assert snapshot.metadata["membership_group_ids"] == [group.pk]
    assert snapshot.metadata["membership_permission_ids"] == [permission.pk]


def test_identity_relations_are_attached_and_survive_a_group_deletion(organization, service):
    """The relation is convenience; the name snapshot is the record.

    Deleting a group takes the link row with it -- which is exactly why the names
    are stored separately. The audit trail still says what the actor could do.
    """
    repository = OrganizationAuditRepository()
    group = Group.objects.create(name="doomed_group")

    identity = repository.resolve_identities(
        [
            IdentitySnapshot(
                identity_type=AuditActorType.MEMBERSHIP,
                identity_key="3",
                group_names=["doomed_group"],
                metadata={"membership_group_ids": [group.pk]},
            )
        ]
    )[0]

    assert list(identity.membership_groups.values_list("name", flat=True)) == ["doomed_group"]

    group.delete()
    identity.refresh_from_db()

    assert list(identity.membership_groups.all()) == []
    assert identity.group_names == ["doomed_group"]


def test_relation_ids_do_not_travel_in_the_snapshot():
    """A primary key means nothing in another database, so it stays home.

    A record synced to a warehouse must not carry ids that resolve to different
    rows there -- the names and keys travel instead.
    """
    repository = OrganizationAuditRepository()
    group = Group.objects.create(name="local_group")

    identity = repository.resolve_identity(
        IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP,
            identity_key="3",
            group_names=["local_group"],
            metadata={"membership_group_ids": [group.pk]},
        )
    )
    snapshot = repository.identity_to_snapshot(identity)

    assert "membership_group_ids" not in snapshot.metadata
    assert snapshot.group_names == ["local_group"]

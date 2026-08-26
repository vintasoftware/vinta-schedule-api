"""Filtering audit records by columns that only this project's models have.

``AuditQuery`` cannot name ``users.User.email`` -- it is portable by design, and
that column exists only because this project swapped in its own identity model.
``OrganizationAuditQuery`` plus ``OrganizationAuditRepository._filtered_queryset``
are the supported way to reach it. These tests are the worked example.
"""

from django.contrib.auth.models import Group, Permission

import pytest
from model_bakery import baker
from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.filtering import record_matches
from vinta_audit_logs.repositories import InMemoryAuditRepository
from vinta_audit_logs.types import (
    AuditQuery,
    AuditRecordData,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)

from audit_integration.constants import AuditActorType
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import OrganizationAuditService
from audit_integration.types import OrganizationAuditQuery
from organizations.models import Organization, OrganizationMembership
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Query Test Org")


@pytest.fixture
def repository() -> OrganizationAuditRepository:
    return OrganizationAuditRepository()


@pytest.fixture
def service(repository) -> OrganizationAuditService:
    return OrganizationAuditService(repository=repository)


def _membership(organization, email, *, group=None, permission=None):
    user = baker.make(User, email=email)
    membership = OrganizationMembership.objects.create(organization=organization, user=user)
    if group is not None:
        membership.groups.add(group)
    if permission is not None:
        membership.permissions.add(permission)
    return membership


def _record(service, organization, membership, action_key="create") -> AuditRecordData:
    return AuditRecordData(
        action_key=action_key,
        actor=service.actor_from_membership(membership),
        subject=SubjectRef(subject_type="organizations.organization", subject_id="1"),
        scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=str(organization.id)),
    )


def test_find_every_record_by_the_actors_email(repository, service, organization):
    """The question this exists to answer: what did hugo@vinta.com.br do?"""
    hugo = _membership(organization, "hugo@vinta.com.br")
    someone_else = _membership(organization, "other@example.com")
    repository.bulk_add(
        [
            _record(service, organization, hugo),
            _record(service, organization, hugo, action_key="update"),
            _record(service, organization, someone_else),
        ]
    )

    page = repository.query(
        OrganizationAuditQuery(actor_user_emails=["hugo@vinta.com.br"]), limit=50
    )

    assert page.total == 2
    assert {record.action_key for record in page.items} == {"create", "update"}


def test_the_email_match_is_case_insensitive(repository, service, organization):
    """Email is case-insensitive, so a filter on it must be too."""
    _membership(organization, "Hugo@Vinta.com.BR")
    repository.bulk_add([_record(service, organization, OrganizationMembership.objects.first())])

    page = repository.query(
        OrganizationAuditQuery(actor_user_emails=["hugo@vinta.com.br"]), limit=50
    )

    assert page.total == 1


def test_scoping_the_email_filter_keeps_it_on_the_index(repository, service, organization):
    """Pair a project filter with a portable one: cut on the index, then join."""
    other_org = Organization.objects.create(name="Elsewhere")
    # One person, two organizations: the same membership user acting in both.
    # (``users_user.email`` is unique, so this cannot be two users sharing one.)
    hugo_here = _membership(organization, "hugo@vinta.com.br")
    hugo_there = OrganizationMembership.objects.create(organization=other_org, user=hugo_here.user)
    repository.bulk_add(
        [_record(service, organization, hugo_here), _record(service, other_org, hugo_there)]
    )

    page = repository.query(
        OrganizationAuditQuery(
            scope_keys=[str(organization.id)],
            actor_user_emails=["hugo@vinta.com.br"],
        ),
        limit=50,
    )

    assert page.total == 1
    assert page.items[0].scope.scope_key == str(organization.id)


def test_find_records_by_the_group_the_actor_held(repository, service, organization):
    """ "Everything done by anyone who was an admin at the time.\""""
    admins, _ = Group.objects.get_or_create(name="audit_query_admins")
    admin = _membership(organization, "admin@example.com", group=admins)
    plain = _membership(organization, "plain@example.com")
    repository.bulk_add(
        [_record(service, organization, admin), _record(service, organization, plain)]
    )

    page = repository.query(OrganizationAuditQuery(actor_group_ids=[admins.pk]), limit=50)

    assert page.total == 1
    assert page.items[0].actor.group_names == ["audit_query_admins"]


def test_find_records_by_the_permission_the_actor_held(repository, service, organization):
    """The question an incident review asks: who could do this, and did?"""
    permission = Permission.objects.first()
    holder = _membership(organization, "holder@example.com", permission=permission)
    plain = _membership(organization, "plain@example.com")
    repository.bulk_add(
        [_record(service, organization, holder), _record(service, organization, plain)]
    )

    page = repository.query(
        OrganizationAuditQuery(actor_permission_codenames=[permission.codename]), limit=50
    )

    assert page.total == 1


def test_organization_ids_is_the_cheap_one(repository, service, organization):
    """Filtering by organization still lands on the scope_key index, no join."""
    q = OrganizationAuditQuery(organization_ids=[organization.id])
    sql = str(repository._filtered_queryset(q).query)

    assert "JOIN" not in sql.upper()
    assert f"'{organization.id}'" in sql or str(organization.id) in sql


def test_a_plain_audit_query_takes_the_unchanged_fast_path(repository):
    """Nothing pays for the project filters unless it asks for them."""
    portable_sql = str(repository._filtered_queryset(AuditQuery(actions=["create"])).query)
    extended_sql = str(
        repository._filtered_queryset(OrganizationAuditQuery(actions=["create"])).query
    )

    assert portable_sql == extended_sql
    assert "JOIN" not in portable_sql.upper()


def test_an_empty_project_filter_matches_nothing(repository, service, organization):
    """``[]`` keeps its meaning in the extended filters too."""
    membership = _membership(organization, "someone@example.com")
    repository.bulk_add([_record(service, organization, membership)])

    assert repository.query(OrganizationAuditQuery(actor_user_emails=[])).total == 0
    assert repository.query(OrganizationAuditQuery(actor_user_emails=None)).total == 1


def test_a_backend_that_cannot_apply_these_filters_refuses_to_guess():
    """Silence is the failure mode worth preventing.

    Handing a project query to a backend that does not know the extra fields
    would return records that look filtered and are not -- more than the caller
    asked for, with nothing to indicate it. Every backend that filters in Python
    goes through ``record_matches``, so the guard sits there.
    """
    memory = InMemoryAuditRepository()
    memory.bulk_add(
        [
            AuditRecordData(
                action_key="create",
                actor=IdentitySnapshot(identity_type=AuditActorType.SYSTEM),
                subject=SubjectRef(subject_type="organizations.organization", subject_id="1"),
            )
        ]
    )

    with pytest.raises(NotImplementedError, match="actor_user_emails"):
        memory.query(OrganizationAuditQuery(actor_user_emails=["hugo@vinta.com.br"]))


def test_the_guard_lets_a_project_query_through_when_it_uses_only_portable_fields():
    """An unset extension field is not an active one."""
    memory = InMemoryAuditRepository()
    stored = memory.bulk_add(
        [
            AuditRecordData(
                action_key="create",
                actor=IdentitySnapshot(identity_type=AuditActorType.SYSTEM),
                subject=SubjectRef(subject_type="organizations.organization", subject_id="1"),
            )
        ]
    )

    assert record_matches(stored[0], OrganizationAuditQuery(actions=["create"])) is True

"""``audit`` under implicit organization scoping.

The audit trail is the awkward case for tenant scoping: it is written from
wherever a business action happens (a request, a Celery task, a management
command) and read almost exclusively from a *staff* context that has no
membership and therefore no organization. Both halves are pinned here.

* **Writes name their organization.** ``AuditRecordData.organization_id`` is the
  organization the record belongs to, and it is not always the one bound: the
  audit task rebuilds it from a queued payload and binds it itself, and the
  repository passes it to both ``Audit.objects.create`` and the through-table
  ``bulk_create``. A record must land where the *caller said*, never where the
  ambient context happens to point.
* **Reads through ``objects`` are scoped, and the repository does not use them.**
  ``DjangoORMAuditRepository`` reads through ``original_manager`` on purpose so
  the staff admin can see across organizations; the scoped manager is proved
  here to be genuinely scoped so that "why not just use ``objects``" has an
  answer on the record.
"""

from typing import Any

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from vinta_orgs.exceptions import OrganizationNotFoundError

from audit.constants import AuditAction, AuditActorType
from audit.factories import AuditFactory
from audit.models import Audit, AuditAffectedMembership
from audit.types import ActorSnapshot, AuditRecordData, SubjectRef
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationMembership


User = get_user_model()


def _repository() -> Any:
    from di_core.containers import container

    assert container is not None  # noqa: S101 -- the DI container is wired by AppConfig.ready
    return container.audit_repository()


def _record_data(organization: Organization, **overrides: Any) -> AuditRecordData:
    defaults: dict[str, Any] = {
        "organization_id": organization.pk,
        "action": AuditAction.CREATE,
        "actor": ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
        "subject": SubjectRef(
            subject_type="organizations.Organization",
            subject_id=str(organization.pk),
            subject_label=organization.name,
        ),
        "affected_membership_ids": (),
        "diff": None,
    }
    defaults.update(overrides)
    return AuditRecordData(**defaults)


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Audit Org")


@pytest.fixture
def other_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Other Audit Org")


@pytest.mark.django_db
class TestWritesLandInTheOrganizationTheCallerNamed:
    def test_a_write_with_nothing_bound_still_lands(self, organization: Organization) -> None:
        """The repository names ``organization_id``, so it needs no ambient binding.

        This is what lets ``persist_audit_record`` run in a worker and the admin
        export run for a staff user: neither has a membership to bind from.
        """
        record = _repository().add(_record_data(organization))

        stored = Audit.original_manager.get(pk=record.id)
        assert stored.organization_id == organization.pk

    def test_a_write_ignores_a_conflicting_binding(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        """The named organization wins over the bound one.

        ``AuditService.record`` is called from inside business services that are
        themselves running bound -- and, for a reseller acting on a child, bound
        to a *different* organization than the record belongs to. Pinned because
        the failure mode is a record filed under the wrong tenant, which no
        error surfaces.
        """
        with organization_context(other_organization):
            record = _repository().add(_record_data(organization))

        stored = Audit.original_manager.get(pk=record.id)
        assert stored.organization_id == organization.pk
        assert stored.organization_id != other_organization.pk

    def test_affected_membership_rows_land_in_the_same_organization(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        user = baker.make(User)
        OrganizationMembership.objects.create(user=user, organization=organization)

        with organization_context(other_organization):
            record = _repository().add(
                _record_data(organization, affected_membership_ids=[user.pk])
            )

        links = list(AuditAffectedMembership.original_manager.filter(audit_fk_id=record.id))
        assert len(links) == 1
        assert links[0].organization_id == organization.pk
        assert links[0].membership_user_id == user.pk

    def test_the_link_resolves_through_the_organization_safe_relation(
        self, organization: Organization
    ) -> None:
        """``link.membership`` joins on ``(membership_user_id, organization)``.

        The concrete column alone would match a same-user membership in another
        organization; the safe relation must not.
        """
        user = baker.make(User)
        membership = OrganizationMembership.objects.create(user=user, organization=organization)
        record = _repository().add(_record_data(organization, affected_membership_ids=[user.pk]))

        link = AuditAffectedMembership.original_manager.get(audit_fk_id=record.id)
        assert link.membership == membership


@pytest.mark.django_db
class TestReadsThroughObjectsAreScoped:
    def test_an_unbound_read_raises_rather_than_returning_nothing(
        self, organization: Organization
    ) -> None:
        AuditFactory().create(organization=organization)

        with pytest.raises(OrganizationNotFoundError):
            Audit.objects.count()

    def test_a_bound_read_sees_only_its_own_organizations_records(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        mine = AuditFactory().create(organization=organization)
        theirs = AuditFactory().create(organization=other_organization)

        with organization_context(organization):
            visible = set(Audit.objects.values_list("pk", flat=True))

        assert mine.pk in visible
        assert theirs.pk not in visible

        # ...and the control: the repository's own manager sees both, which is
        # why it -- not ``objects`` -- is what the staff-facing reads use.
        both = set(Audit.original_manager.values_list("pk", flat=True))
        assert {mine.pk, theirs.pk} <= both

    def test_the_through_table_is_scoped_too(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        mine_user = baker.make(User)
        theirs_user = baker.make(User)
        OrganizationMembership.objects.create(user=mine_user, organization=organization)
        OrganizationMembership.objects.create(user=theirs_user, organization=other_organization)

        mine = _repository().add(_record_data(organization, affected_membership_ids=[mine_user.pk]))
        theirs = _repository().add(
            _record_data(other_organization, affected_membership_ids=[theirs_user.pk])
        )

        with organization_context(organization):
            visible = set(AuditAffectedMembership.objects.values_list("audit_fk_id", flat=True))

        assert mine.id in visible
        assert theirs.id not in visible


@pytest.mark.django_db
class TestTheManyToManyCarriesTheOrganization:
    """``Audit.affected_memberships`` joins through the safe relation on both hops.

    Its ``through_fields`` name ``audit`` (the ``OrganizationSafeForeignKey``
    descriptor), not ``audit_fk``, so the first hop's ``ON`` clause matches on
    ``(audit_fk, organization)``. Proved by planting a through row whose
    ``audit_fk`` matches but whose organization does not: the safe join must not
    see it, while the concrete column does.
    """

    def test_a_cross_organization_through_row_is_not_traversed(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        user = baker.make(User)
        OrganizationMembership.objects.create(user=user, organization=other_organization)
        audit = AuditFactory().create(organization=organization)

        # A row that points at ``audit`` by key but belongs to another
        # organization -- the shape the safe join exists to refuse.
        AuditAffectedMembership.objects.create(
            organization=other_organization,
            audit_fk=audit,
            membership_user_id=user.pk,
        )

        # The control: the concrete key *does* reach it, so the assertion below
        # cannot pass because the row is simply absent.
        assert AuditAffectedMembership.original_manager.filter(audit_fk_id=audit.pk).count() == 1

        assert audit.affected_memberships.count() == 0
        assert audit.affected_membership_links.count() == 0


@pytest.mark.django_db
class TestTheAuditTaskBindsTheRecordsOwnOrganization:
    def test_the_task_persists_under_the_payload_organization(
        self, organization: Organization
    ) -> None:
        """``persist_audit_record`` runs in a worker with nothing bound.

        Its body is wrapped in ``organization_context(...)`` built from the
        payload; this pins that the row still lands in the payload's
        organization now that the manager reads that binding.
        """
        import dataclasses

        from audit.tasks import persist_audit_record

        payload = dataclasses.asdict(_record_data(organization))

        persist_audit_record(payload)

        stored = Audit.original_manager.filter(organization_id=organization.pk)
        assert stored.count() == 1
        first = stored.first()
        assert first is not None
        assert first.subject_id == str(organization.pk)

"""How a public API token's on-behalf-of claim reaches the audit record.

Records go through ``OrganizationAuditService.record`` and the real dispatcher,
which persists them in-process under the test settings
(``CELERY_TASK_ALWAYS_EAGER``), and are read back from the repository.
"""

import uuid

import pytest
from model_bakery import baker
from vinta_audit_logs.types import AuditQuery, IdentitySnapshot, SubjectRef

from audit_integration.constants import AuditActorType
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import IDENTITY_LABEL_MAX_LENGTH, OrganizationAuditService
from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService
from users.models import User


pytestmark = pytest.mark.django_db

SUBJECT = SubjectRef(subject_type="organizations.organization", subject_id="1")


class SimulatedFailureError(Exception):
    """Raised inside a claim block to check the claim is unbound on the way out."""


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="On Behalf Of Org")


@pytest.fixture
def repository() -> OrganizationAuditRepository:
    return OrganizationAuditRepository()


@pytest.fixture
def service(repository: OrganizationAuditRepository) -> OrganizationAuditService:
    return OrganizationAuditService(repository=repository)


def _token(organization: Organization) -> SystemUser:
    system_user, _ = PublicAPIAuthService().create_system_user(
        integration_name=f"test_{uuid.uuid4().hex[:8]}",
        organization=organization,
    )
    return system_user


@pytest.fixture
def token(organization: Organization) -> SystemUser:
    return _token(organization)


def _member(organization: Organization, email: str) -> OrganizationMembership:
    user = baker.make(User, email=email)
    return OrganizationMembership.objects.create(organization=organization, user=user)


@pytest.fixture
def member(organization: Organization) -> OrganizationMembership:
    return _member(organization, "ana@example.com")


@pytest.fixture
def write(service, repository, organization, django_capture_on_commit_callbacks):
    """Record one audit entry with ``actor`` and return it as stored."""

    def write(actor: IdentitySnapshot, **kwargs):
        with django_capture_on_commit_callbacks(execute=True):
            service.record(
                action="create",
                actor=actor,
                subject=SUBJECT,
                scope=service.scope_from_organization_id(organization.id),
                **kwargs,
            )
        page = repository.query(AuditQuery(), limit=50)
        return max(page.items, key=lambda record: record.id)

    return write


class TestRecord:
    def test_without_a_claim_nothing_changes(self, service, write, token):
        stored = write(service.actor_from_system_user(token))

        assert stored.actor.identity_label == f"system-user:{token.id}"
        assert stored.on_behalf_of is None

    def test_a_claim_by_the_acting_token_fills_on_behalf_of(self, service, write, token, member):
        with service.on_behalf_of_member(token, member):
            stored = write(service.actor_from_system_user(token))

        assert stored.actor.identity_type == AuditActorType.SYSTEM_USER
        assert stored.actor.identity_key == str(token.id)
        assert stored.actor.identity_label == (
            f"system-user:{token.id} on behalf of ana@example.com (claimed)"
        )
        assert stored.on_behalf_of is not None
        assert stored.on_behalf_of.identity_type == AuditActorType.CLAIMED_MEMBERSHIP
        assert stored.on_behalf_of.identity_key == str(member.user_id)
        assert stored.on_behalf_of.identity_label == "ana@example.com"
        assert stored.on_behalf_of.user_id == member.user_id
        assert stored.on_behalf_of.metadata == {"claimed_by_system_user_id": token.id}

    def test_a_claim_by_another_token_is_ignored(self, service, write, organization, token, member):
        other_token = _token(organization)

        with service.on_behalf_of_member(other_token, member):
            stored = write(service.actor_from_system_user(token))

        assert stored.actor.identity_label == f"system-user:{token.id}"
        assert stored.on_behalf_of is None

    def test_a_non_token_actor_with_the_same_key_is_ignored(self, service, write, token, member):
        """A membership actor whose user id happens to equal the token id is not the token."""
        actor = IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP,
            identity_key=str(token.id),
            identity_label="someone",
        )

        with service.on_behalf_of_member(token, member):
            stored = write(actor)

        assert stored.actor.identity_label == "someone"
        assert stored.on_behalf_of is None

    def test_an_explicit_on_behalf_of_is_kept(self, service, write, token, member):
        explicit = IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP, identity_key="42", identity_label="explicit"
        )

        with service.on_behalf_of_member(token, member):
            stored = write(service.actor_from_system_user(token), on_behalf_of=explicit)

        assert stored.actor.identity_label == f"system-user:{token.id}"
        assert stored.on_behalf_of is not None
        assert stored.on_behalf_of.identity_label == "explicit"

    def test_a_long_member_label_still_fits_the_column(self, service, write, organization, token):
        long_email = f"{'a' * 240}@example.com"
        member = _member(organization, long_email)

        with service.on_behalf_of_member(token, member):
            stored = write(service.actor_from_system_user(token))

        assert len(stored.actor.identity_label) == IDENTITY_LABEL_MAX_LENGTH
        assert stored.actor.identity_label.startswith(f"system-user:{token.id} on behalf of aaa")
        assert stored.on_behalf_of is not None
        assert stored.on_behalf_of.identity_label == long_email


class TestClaimLifetime:
    def test_the_claim_ends_with_the_block(self, service, write, token, member):
        with service.on_behalf_of_member(token, member):
            pass

        stored = write(service.actor_from_system_user(token))

        assert stored.on_behalf_of is None

    def test_the_claim_ends_after_an_exception(self, service, write, token, member):
        with pytest.raises(SimulatedFailureError), service.on_behalf_of_member(token, member):
            raise SimulatedFailureError

        stored = write(service.actor_from_system_user(token))

        assert stored.on_behalf_of is None

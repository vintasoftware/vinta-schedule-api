"""How a public API token's on-behalf-of claim reaches the audit record."""

import uuid
from unittest.mock import MagicMock

import pytest
from model_bakery import baker
from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.types import AuditQuery, AuditRecordData, IdentitySnapshot, SubjectRef

from audit_integration.constants import AuditActorType
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import OrganizationAuditService
from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser
from public_api.on_behalf_of import (
    OnBehalfOfClaim,
    current_on_behalf_of_claim,
    on_behalf_of_claim,
)
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
def dispatcher() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(repository: OrganizationAuditRepository, dispatcher: MagicMock):
    service = OrganizationAuditService(repository=repository)
    service.get_dispatcher = lambda: dispatcher  # type: ignore[method-assign]
    return service


@pytest.fixture
def token(organization: Organization) -> SystemUser:
    system_user, _ = PublicAPIAuthService().create_system_user(
        integration_name=f"test_{uuid.uuid4().hex[:8]}",
        organization=organization,
    )
    return system_user


@pytest.fixture
def member(organization: Organization) -> OrganizationMembership:
    user = baker.make(User, email="ana@example.com")
    return OrganizationMembership.objects.create(organization=organization, user=user)


def _claim(token: SystemUser, member: OrganizationMembership) -> OnBehalfOfClaim:
    return OnBehalfOfClaim(
        system_user_id=token.id,
        membership_user_id=member.user_id,
        label="ana@example.com",
    )


def _record(service, organization, actor, *, django_capture_on_commit_callbacks, **kwargs):
    with django_capture_on_commit_callbacks(execute=True):
        service.record(
            action="create",
            actor=actor,
            subject=SUBJECT,
            scope=service.scope_from_organization_id(organization.id),
            **kwargs,
        )


class TestRecord:
    def test_without_a_claim_nothing_changes(
        self, service, dispatcher, organization, token, django_capture_on_commit_callbacks
    ):
        actor = service.actor_from_system_user(token)

        _record(
            service,
            organization,
            actor,
            django_capture_on_commit_callbacks=django_capture_on_commit_callbacks,
        )

        payload = dispatcher.call_args.args[0]
        assert payload["actor"]["identity_label"] == f"system-user:{token.id}"
        assert payload["on_behalf_of"] is None

    def test_a_claim_by_the_acting_token_fills_on_behalf_of(
        self, service, dispatcher, organization, token, member, django_capture_on_commit_callbacks
    ):
        actor = service.actor_from_system_user(token)

        with on_behalf_of_claim(_claim(token, member)):
            _record(
                service,
                organization,
                actor,
                django_capture_on_commit_callbacks=django_capture_on_commit_callbacks,
            )

        payload = dispatcher.call_args.args[0]
        assert payload["actor"]["identity_type"] == AuditActorType.SYSTEM_USER
        assert payload["actor"]["identity_key"] == str(token.id)
        assert payload["actor"]["identity_label"] == (
            f"system-user:{token.id} on behalf of ana@example.com (claimed)"
        )
        on_behalf_of = payload["on_behalf_of"]
        assert on_behalf_of["identity_type"] == AuditActorType.CLAIMED_MEMBERSHIP
        assert on_behalf_of["identity_key"] == str(member.user_id)
        assert on_behalf_of["identity_label"] == "ana@example.com"
        assert on_behalf_of["user_id"] == member.user_id
        assert on_behalf_of["metadata"] == {"claimed_by_system_user_id": token.id}

    def test_a_claim_by_another_token_is_ignored(
        self, service, dispatcher, organization, token, member, django_capture_on_commit_callbacks
    ):
        other_token, _ = PublicAPIAuthService().create_system_user(
            integration_name=f"test_{uuid.uuid4().hex[:8]}",
            organization=organization,
        )
        actor = service.actor_from_system_user(token)

        with on_behalf_of_claim(_claim(other_token, member)):
            _record(
                service,
                organization,
                actor,
                django_capture_on_commit_callbacks=django_capture_on_commit_callbacks,
            )

        payload = dispatcher.call_args.args[0]
        assert payload["actor"]["identity_label"] == f"system-user:{token.id}"
        assert payload["on_behalf_of"] is None

    def test_a_non_token_actor_with_the_same_key_is_ignored(
        self, service, dispatcher, organization, token, member, django_capture_on_commit_callbacks
    ):
        """A membership actor whose user id happens to equal the token id is not the token."""
        actor = IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP,
            identity_key=str(token.id),
            identity_label="someone",
        )

        with on_behalf_of_claim(_claim(token, member)):
            _record(
                service,
                organization,
                actor,
                django_capture_on_commit_callbacks=django_capture_on_commit_callbacks,
            )

        payload = dispatcher.call_args.args[0]
        assert payload["actor"]["identity_label"] == "someone"
        assert payload["on_behalf_of"] is None

    def test_an_explicit_on_behalf_of_is_kept(
        self, service, dispatcher, organization, token, member, django_capture_on_commit_callbacks
    ):
        explicit = IdentitySnapshot(
            identity_type=AuditActorType.MEMBERSHIP, identity_key="42", identity_label="explicit"
        )
        actor = service.actor_from_system_user(token)

        with on_behalf_of_claim(_claim(token, member)):
            _record(
                service,
                organization,
                actor,
                on_behalf_of=explicit,
                django_capture_on_commit_callbacks=django_capture_on_commit_callbacks,
            )

        payload = dispatcher.call_args.args[0]
        assert payload["actor"]["identity_label"] == f"system-user:{token.id}"
        assert payload["on_behalf_of"]["identity_label"] == "explicit"


class TestClaimContext:
    def test_the_claim_is_unbound_after_the_block(self, token, member):
        claim = _claim(token, member)

        with on_behalf_of_claim(claim):
            assert current_on_behalf_of_claim() == claim

        assert current_on_behalf_of_claim() is None

    def test_the_claim_is_unbound_after_an_exception(self, token, member):
        with pytest.raises(SimulatedFailureError), on_behalf_of_claim(_claim(token, member)):
            raise SimulatedFailureError

        assert current_on_behalf_of_claim() is None


class TestStoredClaim:
    def test_the_claim_round_trips_through_the_repository(
        self, repository, service, organization, token, member
    ):
        with on_behalf_of_claim(_claim(token, member)):
            actor, on_behalf_of = service._apply_on_behalf_of_claim(
                service.actor_from_system_user(token)
            )
        repository.bulk_add(
            [
                AuditRecordData(
                    action_key="create",
                    actor=actor,
                    on_behalf_of=on_behalf_of,
                    subject=SUBJECT,
                    scope=service.scope_from_organization_id(organization.id),
                )
            ]
        )

        [stored] = repository.query(AuditQuery(), limit=10).items

        assert stored.actor.identity_type == AuditActorType.SYSTEM_USER
        assert stored.actor.identity_label == (
            f"system-user:{token.id} on behalf of ana@example.com (claimed)"
        )
        assert stored.on_behalf_of is not None
        assert stored.on_behalf_of.identity_type == AuditActorType.CLAIMED_MEMBERSHIP
        assert stored.on_behalf_of.identity_key == str(member.user_id)
        assert stored.on_behalf_of.user_id == member.user_id
        assert stored.on_behalf_of.metadata == {"claimed_by_system_user_id": token.id}
        assert stored.scope.scope_type == ScopeType.SCOPED

"""The ``X-Public-Api-On-Behalf-Of`` header, through the real ``/graphql/`` endpoint.

The mutation used throughout is ``createBookingPolicy``: it writes one audit
record with the token as the actor, which is what the claim attaches to. Audit
records are queued on commit, so each test patches the persisting task and
reads the actor from the payload it was handed.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from django.test import TestCase

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import BookingPolicy, Calendar
from organizations.models import Organization, OrganizationMembership
from organizations.tests.helpers import make_membership
from public_api.constants import ON_BEHALF_OF_HEADER, OnBehalfOfErrorCode, PublicAPIResources
from public_api.exceptions import OnBehalfOfSwitchOnScopedTokenError
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService
from users.models import User


pytestmark = pytest.mark.django_db

CREATE_DEFAULT_POLICY = """
mutation {
    createBookingPolicy(input: {isOrganizationDefault: true, leadTimeSeconds: 60}) {
        success
    }
}
"""

CREATE_TWO_POLICIES = """
mutation CreateTwo($calendarId: Int!) {
    first: createBookingPolicy(input: {isOrganizationDefault: true}) { success }
    second: createBookingPolicy(input: {calendarId: $calendarId}) { success }
}
"""

CREATE_CALENDAR_POLICY = """
mutation CreateForCalendar($calendarId: Int!) {
    createBookingPolicy(input: {calendarId: $calendarId}) { success }
}
"""

CREATE_MEMBER_POLICY = """
mutation CreateForMember($membershipUserId: Int!) {
    createBookingPolicy(input: {membershipUserId: $membershipUserId}) { success }
}
"""

CREATE_SYSTEM_USER_TOKEN = """
mutation Mint($input: CreateSystemUserTokenInput!) {
    createSystemUserToken(input: $input) { systemUserId }
}
"""

LIST_POLICIES = """
query { bookingPolicies { id } }
"""


@dataclass
class Token:
    """A minted token and how to call ``/graphql/`` with it."""

    system_user: SystemUser
    secret: str
    auth_service: PublicAPIAuthService

    def post(
        self,
        query: str,
        *,
        variables: dict | None = None,
        on_behalf_of: str | None = None,
        organization_header: int | None = None,
    ) -> dict:
        from di_core.containers import container

        assert container is not None  # noqa: S101
        headers = {"authorization": f"Bearer {self.system_user.id}:{self.secret}"}
        if on_behalf_of is not None:
            headers[ON_BEHALF_OF_HEADER] = on_behalf_of
        if organization_header is not None:
            headers["X-Public-Api-Organization-Id"] = str(organization_header)
        # Audit records are queued on commit; run those callbacks now so the
        # patched task has been called by the time the request returns.
        with (
            container.public_api_auth_service.override(self.auth_service),
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            response = APIClient().post(
                "/graphql/",
                data={"query": query, "variables": variables or {}},
                format="json",
                headers=headers,
            )
        assert response.status_code == 200
        return response.json()


def _mint(
    organization: Organization | None,
    *,
    scoped_to_membership: OrganizationMembership | None = None,
    requires_on_behalf_of: bool = False,
    resources: tuple[str, ...] = (PublicAPIResources.BOOKING_POLICY,),
) -> Token:
    auth_service = PublicAPIAuthService()
    system_user, secret = auth_service.create_system_user(
        integration_name=f"test_{uuid.uuid4().hex[:8]}",
        organization=organization,
        scoped_to_membership=scoped_to_membership,
        requires_on_behalf_of=requires_on_behalf_of,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return Token(system_user=system_user, secret=secret, auth_service=auth_service)


def _error_codes(body: dict) -> list[str | None]:
    return [error.get("extensions", {}).get("code") for error in body.get("errors") or []]


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="On Behalf Of Org")


@pytest.fixture
def other_organization() -> Organization:
    return baker.make(Organization, name="Another Org")


@pytest.fixture
def member(organization: Organization) -> OrganizationMembership:
    return make_membership(
        user=baker.make(User, email="ana@example.com"), organization=organization
    )


@pytest.fixture
def audit_task() -> Iterator[MagicMock]:
    with patch("vinta_audit_logs.tasks.persist_audit_record") as task:
        yield task


def _payloads(audit_task: MagicMock) -> list[dict]:
    return [call.args[0] for call in audit_task.delay.call_args_list]


class TestOrganizationWideToken:
    def test_a_valid_member_is_recorded_as_a_claim(self, organization, member, audit_task):
        token = _mint(organization)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(member.user_id))

        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["actor"]["identity_type"] == "system_user"
        assert payload["actor"]["identity_key"] == str(token.system_user.id)
        assert payload["actor"]["identity_label"] == (
            f"system-user:{token.system_user.id} on behalf of ana@example.com (claimed)"
        )
        assert payload["on_behalf_of"]["identity_type"] == "claimed_membership"
        assert payload["on_behalf_of"]["identity_key"] == str(member.user_id)
        assert payload["on_behalf_of"]["identity_label"] == "ana@example.com"

    def test_every_record_in_the_request_carries_the_claim(self, organization, member, audit_task):
        token = _mint(organization)
        calendar = baker.make(Calendar, organization=organization)

        body = token.post(
            CREATE_TWO_POLICIES,
            variables={"calendarId": calendar.id},
            on_behalf_of=str(member.user_id),
        )

        assert body == {
            "data": {"first": {"success": True}, "second": {"success": True}},
        }
        payloads = _payloads(audit_task)
        assert len(payloads) == 2
        assert {payload["on_behalf_of"]["identity_key"] for payload in payloads} == {
            str(member.user_id)
        }

    def test_without_the_header_nothing_changes(self, organization, audit_task):
        token = _mint(organization)

        body = token.post(CREATE_DEFAULT_POLICY)

        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["actor"]["identity_label"] == f"system-user:{token.system_user.id}"
        assert payload["on_behalf_of"] is None

    @pytest.mark.parametrize("header", ["abc", "0", "-1", "", "1.5", "999999999"])
    def test_a_value_that_is_not_a_member_is_rejected(
        self, organization, member, audit_task, header
    ):
        token = _mint(organization)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=header)

        assert body["data"] is None
        assert _error_codes(body) == [OnBehalfOfErrorCode.INVALID.value]
        assert body["errors"][0]["message"] == (
            f"{ON_BEHALF_OF_HEADER} does not name an active member of this organization."
        )
        assert not BookingPolicy.objects.filter_by_organization(organization).exists()
        assert not audit_task.delay.called

    def test_a_member_of_another_organization_is_rejected(
        self, organization, other_organization, audit_task
    ):
        outsider = make_membership(
            user=baker.make(User, email="outsider@example.com"),
            organization=other_organization,
        )
        token = _mint(organization)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(outsider.user_id))

        assert _error_codes(body) == [OnBehalfOfErrorCode.INVALID.value]
        assert not BookingPolicy.objects.filter_by_organization(organization).exists()
        assert not audit_task.delay.called

    def test_an_inactive_member_is_rejected(self, organization, audit_task):
        former = make_membership(
            user=baker.make(User, email="former@example.com"),
            organization=organization,
            is_active=False,
        )
        token = _mint(organization)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(former.user_id))

        assert _error_codes(body) == [OnBehalfOfErrorCode.INVALID.value]
        assert not audit_task.delay.called

    def test_a_query_ignores_an_invalid_header(self, organization):
        token = _mint(organization)

        body = token.post(LIST_POLICIES, on_behalf_of="abc")

        assert body == {"data": {"bookingPolicies": []}}

    def test_the_claim_does_not_outlive_its_request(self, organization, member, audit_task):
        token = _mint(organization)
        calendar = baker.make(Calendar, organization=organization)
        token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(member.user_id))
        # This one fails inside the resolver (one default per organization).
        failed = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(member.user_id))
        audit_task.reset_mock()

        body = token.post(CREATE_CALENDAR_POLICY, variables={"calendarId": calendar.id})

        assert failed.get("errors")
        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["actor"]["identity_label"] == f"system-user:{token.system_user.id}"
        assert payload["on_behalf_of"] is None

    def test_a_minted_token_is_recorded_as_created_for_the_member(
        self, organization, member, audit_task
    ):
        organization.can_invite_organizations = True
        organization.save(update_fields=["can_invite_organizations"])
        token = _mint(organization, resources=(PublicAPIResources.SYSTEM_USER,))

        body = token.post(
            CREATE_SYSTEM_USER_TOKEN,
            variables={
                "input": {
                    "organizationId": str(organization.id),
                    "integrationName": "minted-for-ana",
                    "resources": [PublicAPIResources.CALENDAR],
                }
            },
            on_behalf_of=str(member.user_id),
        )

        assert "errors" not in body, body
        [payload] = _payloads(audit_task)
        assert payload["action_key"] == "create"
        assert payload["actor"]["identity_key"] == str(token.system_user.id)
        assert payload["on_behalf_of"]["identity_key"] == str(member.user_id)


class TestMemberScopedToken:
    def test_the_header_is_ignored_and_the_write_is_attributed_as_before(
        self, organization, member, audit_task
    ):
        dan = make_membership(
            user=baker.make(User, email="dan@example.com"), organization=organization
        )
        token = _mint(organization, scoped_to_membership=dan)

        body = token.post(
            CREATE_MEMBER_POLICY,
            variables={"membershipUserId": dan.user_id},
            on_behalf_of=str(member.user_id),
        )

        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["actor"]["identity_label"] == f"system-user:{token.system_user.id}"
        assert payload["actor"]["metadata"]["system_user_scoped_to_membership"] == dan.user_id
        assert payload["on_behalf_of"] is None


class TestOrgLessToken:
    def test_a_member_of_the_chosen_organization_is_recorded(
        self, organization, member, audit_task
    ):
        token = _mint(None)

        body = token.post(
            CREATE_DEFAULT_POLICY,
            on_behalf_of=str(member.user_id),
            organization_header=organization.id,
        )

        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["on_behalf_of"]["identity_key"] == str(member.user_id)

    def test_a_member_of_a_different_organization_is_rejected(
        self, organization, other_organization, member, audit_task
    ):
        token = _mint(None)

        body = token.post(
            CREATE_DEFAULT_POLICY,
            on_behalf_of=str(member.user_id),
            organization_header=other_organization.id,
        )

        assert _error_codes(body) == [OnBehalfOfErrorCode.INVALID.value]
        assert not BookingPolicy.objects.filter_by_organization(other_organization).exists()
        assert not audit_task.delay.called

    def test_without_a_chosen_organization_the_header_is_ignored(self, member):
        token = _mint(None)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of="abc")

        assert OnBehalfOfErrorCode.INVALID.value not in _error_codes(body)


class TestRequiresOnBehalfOf:
    def test_a_mutation_without_the_header_is_rejected(self, organization, audit_task):
        token = _mint(organization, requires_on_behalf_of=True)

        body = token.post(CREATE_DEFAULT_POLICY)

        assert body["data"] is None
        assert _error_codes(body) == [OnBehalfOfErrorCode.REQUIRED.value]
        assert not BookingPolicy.objects.filter_by_organization(organization).exists()
        assert not audit_task.delay.called

    def test_a_query_without_the_header_is_answered(self, organization):
        token = _mint(organization, requires_on_behalf_of=True)

        body = token.post(LIST_POLICIES)

        assert body == {"data": {"bookingPolicies": []}}

    def test_a_mutation_naming_a_member_succeeds(self, organization, member, audit_task):
        token = _mint(organization, requires_on_behalf_of=True)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of=str(member.user_id))

        assert body == {"data": {"createBookingPolicy": {"success": True}}}
        [payload] = _payloads(audit_task)
        assert payload["on_behalf_of"]["identity_key"] == str(member.user_id)

    def test_an_invalid_member_is_reported_as_invalid(self, organization):
        token = _mint(organization, requires_on_behalf_of=True)

        body = token.post(CREATE_DEFAULT_POLICY, on_behalf_of="abc")

        assert _error_codes(body) == [OnBehalfOfErrorCode.INVALID.value]

    def test_an_org_less_token_is_held_to_it(self, organization):
        token = _mint(None, requires_on_behalf_of=True)

        body = token.post(CREATE_DEFAULT_POLICY, organization_header=organization.id)

        assert _error_codes(body) == [OnBehalfOfErrorCode.REQUIRED.value]


class TestRequiresOnBehalfOfService:
    """``PublicAPIAuthService`` is the one write path for the switch."""

    def test_create_refuses_the_switch_with_a_scoped_membership(self, organization, member):
        with pytest.raises(OnBehalfOfSwitchOnScopedTokenError):
            PublicAPIAuthService().create_system_user(
                integration_name="scoped-switch",
                organization=organization,
                scoped_to_membership=member,
                requires_on_behalf_of=True,
            )

        assert not SystemUser.objects.filter_by_organization(organization).exists()

    def test_setting_the_same_value_does_nothing(self, organization, audit_task):
        token = _mint(organization)

        with TestCase.captureOnCommitCallbacks(execute=True):
            changed = token.auth_service.set_requires_on_behalf_of(
                token.system_user, False, acted_by=None
            )

        assert changed is False
        assert not audit_task.delay.called

    def test_set_refuses_the_switch_on_a_scoped_token(self, organization, member):
        token = _mint(organization, scoped_to_membership=member)

        with pytest.raises(OnBehalfOfSwitchOnScopedTokenError):
            token.auth_service.set_requires_on_behalf_of(token.system_user, True, acted_by=None)

        token.system_user.refresh_from_db()
        assert token.system_user.requires_on_behalf_of is False

    def test_an_org_less_token_changes_without_an_audit_record(self, audit_task):
        token = _mint(None)

        with TestCase.captureOnCommitCallbacks(execute=True):
            changed = token.auth_service.set_requires_on_behalf_of(
                token.system_user, True, acted_by=None
            )

        assert changed is True
        stored = SystemUser.original_manager.get(id=token.system_user.id)
        assert stored.requires_on_behalf_of is True
        assert not audit_task.delay.called

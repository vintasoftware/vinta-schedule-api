"""``createInvitation`` takes groups, not a role.

Phase 5 of the vinta-django-orgs migration replaces
``inviteToOrganization(..., role: OrgRole = MEMBER)`` with
``createInvitation(input: { groups: [String!] = ["organization_member"] })`` and
deletes the ``OrgRole`` enum. This is a breaking change for partner
integrations, so what it accepts, what it defaults to and what it refuses are
all pinned here.

The invitation still records a ``role`` column until Phase 6 drops it; the
mutation translates. These tests assert on the *stored* state as well as on the
response, because the translation is the part that could silently invert.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationInvitation, OrganizationRole
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
)
from public_api.models import ResourceAccess
from public_api.schema import schema
from public_api.services import PublicAPIAuthService


CREATE_INVITATION_MUTATION = """
mutation CreateInvitation($input: CreateInvitationInput!) {
    createInvitation(input: $input) {
        invitation { id email }
        token
    }
}
"""


@pytest.fixture
def reseller(db) -> Organization:
    return baker.make(Organization, name="Reseller", can_invite_organizations=True)


@pytest.fixture
def child(db, reseller) -> Organization:
    return baker.make(Organization, name="Child Org", parent=reseller)


@pytest.fixture
def invite(db, reseller):
    """POST ``createInvitation`` as a reseller token holding the invitation scope."""
    from di_core.containers import container

    # ``container`` is declared ``AppContainer | None`` (it is wired at app
    # startup); every request-serving path has it set by the time a test runs.
    assert container is not None
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="test_integration", organization=reseller
    )
    baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")
    client = APIClient()

    def _invite(organization: Organization, email: str, **input_fields) -> dict:
        variables = {
            "input": {
                "userEmail": email,
                "organizationId": str(organization.id),
                **input_fields,
            }
        }
        with (
            container.public_api_auth_service.override(auth_service),
            patch("organizations.services.NotificationService.create_one_off_notification"),
        ):
            response = client.post(
                "/graphql/",
                data={"query": CREATE_INVITATION_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )
        assert response.status_code == 200
        return response.json()

    return _invite


def stored(email: str, organization: Organization) -> OrganizationInvitation:
    return OrganizationInvitation.objects.get(email=email, organization=organization)


class TestTheInputShape:
    """The contract itself, read off the published schema rather than the code."""

    def test_the_input_takes_groups_and_defaults_to_organization_member(self):
        sdl = schema.as_str()

        assert 'groups: [String!]! = ["organization_member"]' in sdl

    def test_the_input_no_longer_takes_a_role(self):
        create_invitation_input = schema._schema.type_map["CreateInvitationInput"]

        assert "role" not in create_invitation_input.fields
        assert "groups" in create_invitation_input.fields

    def test_the_org_role_enum_is_gone_from_the_schema(self):
        assert "OrgRole" not in schema._schema.type_map


@pytest.mark.django_db
class TestAcceptedGroups:
    def test_omitting_groups_invites_a_plain_member(self, invite, child):
        """The default is the least-privileged group, as ``role: MEMBER`` was."""
        result = invite(child, "plain@example.com")

        assert "errors" not in result
        assert stored("plain@example.com", child).role == OrganizationRole.MEMBER

    def test_organization_member_is_accepted_explicitly(self, invite, child):
        result = invite(child, "explicit@example.com", groups=[GROUP_ORGANIZATION_MEMBER])

        assert "errors" not in result
        assert stored("explicit@example.com", child).role == OrganizationRole.MEMBER

    def test_organization_admin_invites_an_administrator(self, invite, child):
        result = invite(child, "boss@example.com", groups=[GROUP_ORGANIZATION_ADMIN])

        assert "errors" not in result
        assert stored("boss@example.com", child).role == OrganizationRole.ADMIN

    def test_the_membership_created_on_acceptance_carries_the_matching_groups(self, invite, child):
        """End to end: the group named on the invitation is the group granted.

        The invitation stores a role and the acceptance path puts the new
        membership in groups; only the pair together delivers what the partner
        asked for, and only this test crosses both.
        """
        from unittest.mock import Mock

        from di_core.containers import container
        from organizations.models import OrganizationMembership
        from organizations.services import OrganizationService
        from users.models import User

        user = baker.make(User, email="boss2@example.com")
        result = invite(
            child, "boss2@example.com", groups=[GROUP_ORGANIZATION_ADMIN], sendEmail=False
        )
        raw_token = result["data"]["createInvitation"]["token"]
        assert raw_token

        with container.calendar_service.override(Mock()):
            OrganizationService().accept_invitation(token=raw_token, user=user)

        membership = OrganizationMembership.objects.get(user=user, organization=child)
        assert set(membership.groups.values_list("name", flat=True)) == {GROUP_ORGANIZATION_ADMIN}


@pytest.mark.django_db
class TestRefusedGroups:
    def test_an_unknown_group_is_refused(self, invite, child):
        result = invite(child, "nobody@example.com", groups=["organization_owner"])

        assert result["errors"]
        assert "organization_owner" in result["errors"][0]["message"]
        assert not OrganizationInvitation.objects.filter(email="nobody@example.com").exists()

    def test_the_billing_owner_group_is_refused_at_invitation_time(self, invite, child):
        """An invitation has no column to carry it -- refused, not silently dropped.

        A caller who asked for it and got a 200 would believe they had granted
        billing rights to a member who does not hold them.
        """
        result = invite(child, "money@example.com", groups=[GROUP_ORGANIZATION_BILLING_OWNER])

        assert result["errors"]
        assert GROUP_ORGANIZATION_BILLING_OWNER in result["errors"][0]["message"]
        assert not OrganizationInvitation.objects.filter(email="money@example.com").exists()

    def test_a_known_group_alongside_an_unknown_one_is_refused_wholesale(self, invite, child):
        result = invite(
            child, "partial@example.com", groups=[GROUP_ORGANIZATION_ADMIN, "superuser"]
        )

        assert result["errors"]
        assert not OrganizationInvitation.objects.filter(email="partial@example.com").exists()

    def test_an_empty_group_list_invites_a_plain_member(self, invite, child):
        """No group named is the same statement as ``organization_member``.

        Refusing it would be defensible too; accepting it keeps the mutation's
        behaviour identical to omitting the field, which is what a client
        sending a computed-and-possibly-empty list will do.
        """
        result = invite(child, "empty@example.com", groups=[])

        assert "errors" not in result
        assert stored("empty@example.com", child).role == OrganizationRole.MEMBER

"""The membership API reports capabilities, and reports the *right* ones.

Phase 5 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
takes ``role`` out of every response and puts a resolved permission list in its
place. Three things have to hold for that to be worth anything:

1. ``role`` is gone -- from these responses and from the published contract as
   a whole, so a future serializer cannot quietly reintroduce it;
2. what replaces it is what the backend *actually* resolves for that caller in
   that organization, not a list assembled from the caller's role by a second
   piece of logic that can drift;
3. it is the **organization** half of that resolution -- not
   ``user.get_all_permissions()``, which would publish a superuser the entire
   ``auth.Permission`` catalog and publish an ordinary user the global grants
   Phase 4 deliberately made inert. Getting this wrong is an information leak
   about capabilities the API will then refuse to honour.
"""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

import pytest
import yaml
from model_bakery import baker
from rest_framework import status

from organizations.auth_backends import (
    OrganizationModelBackend,
    resolve_membership_permissions,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import (
    MANAGE_BILLING,
    MANAGE_BRANDING,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
)
from organizations.tests.helpers import (
    make_admin_membership,
    make_billing_owner_membership,
    make_membership,
)
from users.models import User


REPO_ROOT = Path(__file__).resolve().parents[2]

ADMIN_PERMISSIONS = sorted([MANAGE_MEMBERS, MANAGE_ORGANIZATION, MANAGE_BRANDING, MANAGE_BILLING])


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization, name="Acme Inc")


def _resolved_by_the_backend(membership: OrganizationMembership) -> list[str]:
    """What ``has_organization_permission`` would answer, for every capability.

    Deliberately routed through the backend method the permission classes ask
    (``OrganizationModelBackend.get_membership_permissions``) rather than
    through the batch resolver the serializers use, so the assertion compares
    two independent computations rather than one with itself.
    """
    return sorted(
        OrganizationModelBackend().get_membership_permissions(
            membership.user, membership.organization
        )
    )


@pytest.mark.django_db
class TestTheBatchResolverAgreesWithTheBackend:
    """The serializers' batch resolver and the authorization backend must agree.

    The serializers cannot call the backend per row (that is a membership
    lookup plus two permission queries *per member*), so a second
    implementation exists. This class is the only thing keeping the two from
    drifting -- and drift is not a red test anywhere else: it is a response
    that advertises a capability the API refuses, or hides one it honours.
    """

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(make_admin_membership, id="admin"),
            pytest.param(make_billing_owner_membership, id="billing-owner"),
            pytest.param(make_membership, id="plain-member"),
        ],
    )
    def test_every_membership_shape_resolves_identically(self, organization, build):
        membership = build(user=baker.make(User), organization=organization)

        batched = resolve_membership_permissions([membership])[membership.pk]

        assert batched == _resolved_by_the_backend(membership)

    def test_a_direct_per_membership_grant_is_included_by_both(self, organization):
        """``OrganizationMembership.permissions`` is empty everywhere today.

        It is still one of the two inputs the backend unions, so a resolver
        that read groups alone would under-report the day something writes it.
        """
        membership = make_membership(user=baker.make(User), organization=organization)
        membership.permissions.add(
            Permission.objects.get(
                codename="manage_members",
                content_type=ContentType.objects.get_for_model(OrganizationMembership),
            )
        )

        assert resolve_membership_permissions([membership])[membership.pk] == [MANAGE_MEMBERS]
        assert _resolved_by_the_backend(membership) == [MANAGE_MEMBERS]

    def test_an_inactive_membership_resolves_nothing_either_way(self, organization):
        membership = make_admin_membership(
            user=baker.make(User), organization=organization, is_active=False
        )

        assert resolve_membership_permissions([membership])[membership.pk] == []
        assert _resolved_by_the_backend(membership) == []

    def test_an_inactive_user_resolves_nothing_either_way(self, organization):
        membership = make_admin_membership(
            user=baker.make(User, is_active=False), organization=organization
        )

        assert resolve_membership_permissions([membership])[membership.pk] == []
        assert _resolved_by_the_backend(membership) == []

    def test_a_grant_in_another_organization_is_not_reported_here(self, organization):
        """The organization half means *this* organization's half."""
        user = baker.make(User)
        elsewhere = baker.make(Organization, name="Other Co")
        make_admin_membership(user=user, organization=elsewhere)
        here = make_membership(user=user, organization=organization)

        assert resolve_membership_permissions([here])[here.pk] == []
        assert _resolved_by_the_backend(here) == []


@pytest.mark.django_db
class TestTheGlobalHalfStaysOutOfTheResponse:
    """The escalation Phase 4's review closed, restated as an information leak.

    ``user.get_all_permissions()`` unions the organization half with the user's
    *global* ``user_permissions`` and ``auth.Group`` rows, and
    ``PermissionsMixin`` short-circuits a superuser to the whole catalog.
    Phase 4 made all three inert for authorization. Publishing any of them here
    would tell a client the caller may do things every permission class denies.
    """

    def test_a_global_user_permission_is_not_reported(self, organization):
        user = baker.make(User)
        membership = make_membership(user=user, organization=organization)
        user.user_permissions.add(
            Permission.objects.get(
                codename="manage_members",
                content_type=ContentType.objects.get_for_model(OrganizationMembership),
            )
        )

        assert resolve_membership_permissions([membership])[membership.pk] == []

    def test_membership_of_the_seeded_group_as_a_global_django_group_is_not_reported(
        self, organization
    ):
        """The exact shape the Django user admin's group picker still offers.

        ``users/admin.py`` lists ``organization_admin`` in the user form's
        ``filter_horizontal`` groups widget. Adding a user there grants nothing
        in any organization (Phase 4), and must therefore be reported nowhere.
        """
        user = baker.make(User)
        membership = make_membership(user=user, organization=organization)
        user.groups.add(Group.objects.get(name="organization_admin"))

        assert resolve_membership_permissions([membership])[membership.pk] == []

    def test_a_superuser_gets_their_membership_capabilities_not_the_catalog(self, organization):
        """A superuser who is a plain member of a tenant is a plain member here.

        Recorded in Phase 3 as an observed behaviour of
        ``get_all_permissions()`` and left for this phase to handle: under a
        bound organization it answers the entire ``auth.Permission`` table.
        Publishing that would be a response naming hundreds of capabilities the
        API refuses -- ``IsBillingOwnerOrAdmin`` included, which charges a card.
        """
        superuser = baker.make(User, is_superuser=True, is_staff=True)
        membership = make_membership(user=superuser, organization=organization)

        reported = resolve_membership_permissions([membership])[membership.pk]

        assert reported == []
        # The control: the thing we are deliberately *not* publishing really is
        # the whole catalog, so this test is not vacuously comparing [] to [].
        assert len(superuser.get_all_permissions()) > len(ADMIN_PERMISSIONS)


@pytest.mark.django_db
class TestTheCurrentMembershipEndpoint:
    """``GET /organizations/current/`` -- the caller's own capabilities."""

    def test_an_admin_sees_their_four_capabilities_and_no_role(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        make_admin_membership(user=user, organization=organization)

        response = auth_client.get(reverse("api:Organizations-current"))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "role" not in body
        assert sorted(body["permissions"]) == ADMIN_PERMISSIONS
        assert body["organization"]["id"] == organization.id

    def test_a_plain_member_sees_an_empty_list(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        make_membership(user=user, organization=organization)

        response = auth_client.get(reverse("api:Organizations-current"))

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["permissions"] == []

    def test_a_billing_owner_sees_exactly_manage_billing(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        make_billing_owner_membership(user=user, organization=organization)

        response = auth_client.get(reverse("api:Organizations-current"))

        assert response.json()["permissions"] == [MANAGE_BILLING]

    def test_the_reported_list_is_what_the_backend_resolves(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        membership = make_billing_owner_membership(user=user, organization=organization)

        response = auth_client.get(reverse("api:Organizations-current"))

        assert sorted(response.json()["permissions"]) == _resolved_by_the_backend(membership)


@pytest.mark.django_db
class TestTheMineEndpoint:
    """``GET /organizations/mine/`` -- one capability list per membership."""

    def test_each_row_reports_its_own_organization_s_capabilities(self, auth_client, user):
        administered = baker.make(Organization, name="Administered Co")
        joined = baker.make(Organization, name="Joined Co")
        make_admin_membership(user=user, organization=administered)
        make_membership(user=user, organization=joined)

        response = auth_client.get(reverse("api:Organizations-mine"))

        assert response.status_code == status.HTTP_200_OK
        rows = {row["organization"]["id"]: row for row in response.json()}
        assert "role" not in rows[administered.id]
        assert sorted(rows[administered.id]["permissions"]) == ADMIN_PERMISSIONS
        assert rows[joined.id]["permissions"] == []

    def test_the_batch_does_not_grow_queries_with_the_membership_count(
        self, auth_client, user, django_assert_max_num_queries
    ):
        """The reason a batch resolver exists at all.

        Resolving per row is a membership lookup plus two permission queries
        each; the org switcher is exactly the endpoint where a user has several
        memberships. Pinned as a ceiling rather than an exact count so an
        unrelated query elsewhere in the view does not make this brittle -- what
        matters is that four memberships do not cost four times one.
        """
        for index in range(4):
            make_admin_membership(
                user=user, organization=baker.make(Organization, name=f"Org {index}")
            )

        with django_assert_max_num_queries(20):
            response = auth_client.get(reverse("api:Organizations-mine"))

        assert len(response.json()) == 4


@pytest.mark.django_db
class TestTheMemberListEndpoint:
    """``GET /organization-members/`` -- who the members are, and what they may do."""

    def test_rows_carry_permissions_and_neither_role_nor_is_billing_owner(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        make_admin_membership(user=user, organization=organization)
        plain = make_membership(user=baker.make(User), organization=organization)

        response = auth_client.get(
            reverse("api:OrganizationMembers-list"),
            headers={"X-Organization-Id": str(organization.id)},
        )

        assert response.status_code == status.HTTP_200_OK
        rows = {row["user_id"]: row for row in response.json()["results"]}
        assert "role" not in rows[user.id]
        assert "is_billing_owner" not in rows[user.id]
        assert sorted(rows[user.id]["permissions"]) == ADMIN_PERMISSIONS
        assert rows[plain.user_id]["permissions"] == []

    def test_each_row_matches_what_the_backend_resolves_for_that_member(self, auth_client, user):
        organization = baker.make(Organization, name="Acme Inc")
        make_admin_membership(user=user, organization=organization)
        billing_owner = make_billing_owner_membership(
            user=baker.make(User), organization=organization
        )

        response = auth_client.get(
            reverse("api:OrganizationMembers-list"),
            headers={"X-Organization-Id": str(organization.id)},
        )

        rows = {row["user_id"]: row for row in response.json()["results"]}
        assert sorted(rows[billing_owner.user_id]["permissions"]) == _resolved_by_the_backend(
            billing_owner
        )


class TestNoPublishedResponseCarriesRole:
    """The regression gate: ``role`` cannot come back through any surface.

    Deliberately schema-level rather than a list of endpoints to check. A new
    serializer field, a new GraphQL type, or a revert of one of this phase's
    edits all show up here without anybody remembering to add a case.

    ``schema.yml`` is the committed, CI-verified OpenAPI document (a pre-commit
    hook fails when it is stale), so reading it is equivalent to regenerating
    it and far cheaper than doing so inside a 10-second test timeout.
    """

    @pytest.mark.parametrize("schema_name", ["schema.yml", "schema-auth.yml"])
    def test_no_openapi_schema_declares_a_role_property(self, schema_name):
        document = yaml.safe_load((REPO_ROOT / schema_name).read_text())

        offenders: list[str] = []

        def walk(node: object, path: str) -> None:
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict) and "role" in properties:
                    offenders.append(f"{path}.properties.role")
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(document, schema_name)

        assert offenders == [], (
            f"{schema_name} publishes a 'role' field at: {offenders}. "
            "Membership authorization is reported as 'permissions'; if a new "
            "field genuinely needs the word 'role', it needs a decision, not a "
            "quiet reintroduction."
        )

    def test_the_role_enum_component_is_gone(self):
        document = yaml.safe_load((REPO_ROOT / "schema.yml").read_text())

        assert "RoleEnum" not in document["components"]["schemas"]

    def test_no_public_graphql_type_exposes_a_role_field(self):
        from public_api.schema import schema

        offenders = [
            f"{type_name}.{field_name}"
            for type_name, graphql_type in schema._schema.type_map.items()
            if not type_name.startswith("__") and hasattr(graphql_type, "fields")
            for field_name in graphql_type.fields
            if field_name == "role"
        ]

        assert offenders == [], f"The public GraphQL schema exposes a 'role' field at: {offenders}."

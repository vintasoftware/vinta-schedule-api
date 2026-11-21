"""The ``SystemUser`` admin's uniqueness pre-check runs across every organization.

``SystemUser.integration_name`` carries a ``UNIQUE`` index, which the database
enforces globally -- it knows nothing about organizations. Django's
``ModelForm._post_clean`` probes for a clash through ``_default_manager``, which
on a scoped model is the *scoped* manager. In the admin that manager is wrong
twice over: nothing binds an organization to a staff request, so the probe
raises ``OrganizationNotFoundError`` and 500s the page; and if something did bind
one, the probe would be confined to it and report a name "free" that another
organization already holds -- turning a friendly field error into an
``IntegrityError`` from the ``INSERT``.

``common.models.UnscopedUniqueChecksMixin`` is the one line that fixes both, and
this file is what fails if it is removed.
"""

from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client

import pytest

from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser


User = get_user_model()

ADD_URL = "/super/public_api/systemuser/add/"


@pytest.fixture
def admin_client(db: Any) -> Client:
    user = User.objects.create_superuser(
        email="systemuser-admin@example.com",
        password="adminpassword",  # noqa: S106
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def organization_a(db: Any) -> Organization:
    return Organization.objects.create(name="Admin Org A")


@pytest.fixture
def organization_b(db: Any) -> Organization:
    return Organization.objects.create(name="Admin Org B")


def _add_payload(organization: Organization, integration_name: str) -> dict[str, Any]:
    """The add form plus the ``ResourceAccess`` inline's management form."""
    return {
        "organization": str(organization.pk),
        "integration_name": integration_name,
        "available_resources-TOTAL_FORMS": "0",
        "available_resources-INITIAL_FORMS": "0",
        "available_resources-MIN_NUM_FORMS": "0",
        "available_resources-MAX_NUM_FORMS": "1000",
    }


@pytest.mark.django_db
class TestTheAddFormReportsACrossOrganizationClash:
    def test_a_name_another_organization_holds_is_a_field_error_not_a_500(
        self, admin_client: Client, organization_a: Organization, organization_b: Organization
    ) -> None:
        SystemUser.objects.create(
            organization=organization_a,
            integration_name="shared-name",
            long_lived_token_hash="hash-a",
        )

        response = admin_client.post(
            ADD_URL, _add_payload(organization_b, "shared-name"), follow=False
        )

        # 200 is the re-rendered form; a successful add would be a 302.
        assert response.status_code == 200  # noqa: PLR2004
        form = response.context["adminform"].form
        assert "integration_name" in form.errors, form.errors
        assert SystemUser.original_manager.filter(integration_name="shared-name").count() == 1

    def test_a_free_name_still_saves(
        self, admin_client: Client, organization_a: Organization, organization_b: Organization
    ) -> None:
        """Control: the pre-check is not rejecting everything."""
        SystemUser.objects.create(
            organization=organization_a,
            integration_name="taken-name",
            long_lived_token_hash="hash-a",
        )

        response = admin_client.post(
            ADD_URL, _add_payload(organization_b, "free-name"), follow=False
        )

        assert response.status_code == 302  # noqa: PLR2004
        assert SystemUser.original_manager.filter(
            integration_name="free-name", organization=organization_b
        ).exists()


@pytest.mark.django_db
class TestRequiresOnBehalfOfInTheAdmin:
    """Staff can set the switch, each change is audited, and member-scoped tokens refuse it."""

    def _change_payload(self, system_user: SystemUser, *, requires: bool) -> dict[str, Any]:
        payload = {
            "organization": str(system_user.organization_id or ""),
            "integration_name": system_user.integration_name,
            "available_resources-TOTAL_FORMS": "0",
            "available_resources-INITIAL_FORMS": "0",
            "available_resources-MIN_NUM_FORMS": "0",
            "available_resources-MAX_NUM_FORMS": "1000",
        }
        if requires:
            payload["requires_on_behalf_of"] = "on"
        return payload

    def test_the_add_form_sets_it(self, admin_client: Client, organization_a: Organization):
        payload = {**_add_payload(organization_a, "switch-add"), "requires_on_behalf_of": "on"}

        response = admin_client.post(ADD_URL, payload, follow=False)

        assert response.status_code == 302  # noqa: PLR2004
        system_user = SystemUser.original_manager.get(integration_name="switch-add")
        assert system_user.requires_on_behalf_of is True

    def test_changing_it_saves_and_audits(
        self,
        admin_client: Client,
        organization_a: Organization,
        django_capture_on_commit_callbacks: Any,
    ):
        system_user = SystemUser.objects.create(
            organization=organization_a,
            integration_name="switch-change",
            long_lived_token_hash="hash",
        )

        with (
            patch("vinta_audit_logs.tasks.persist_audit_record") as audit_task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = admin_client.post(
                f"/super/public_api/systemuser/{system_user.pk}/change/",
                self._change_payload(system_user, requires=True),
                follow=False,
            )

        assert response.status_code == 302  # noqa: PLR2004
        system_user.refresh_from_db()
        assert system_user.requires_on_behalf_of is True
        diffs = [call.args[0]["diff"] for call in audit_task.delay.call_args_list]
        assert diffs == [{"requires_on_behalf_of": {"old": False, "new": True}}]

    def test_a_member_scoped_token_refuses_it(
        self, admin_client: Client, organization_a: Organization
    ):
        member = User.objects.create_user(email="scoped-member@example.com", password="x")  # noqa: S106
        OrganizationMembership.objects.create(organization=organization_a, user=member)
        system_user = SystemUser.objects.create(
            organization=organization_a,
            integration_name="switch-scoped",
            long_lived_token_hash="hash",
            scoped_to_membership_user_id=member.id,
        )

        response = admin_client.post(
            f"/super/public_api/systemuser/{system_user.pk}/change/",
            self._change_payload(system_user, requires=True),
            follow=False,
        )

        assert response.status_code == 200  # noqa: PLR2004
        assert "requires_on_behalf_of" in response.context["adminform"].form.errors
        system_user.refresh_from_db()
        assert system_user.requires_on_behalf_of is False

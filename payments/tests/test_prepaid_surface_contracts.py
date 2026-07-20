"""Phase 6c: every *surface* over a guarded pre-paid creation path returns the shared
over-limit contract.

``test_prepaid_resource_coverage.py`` proves each ``kind=prepaid`` resource has a guarded
creation *service*. That rests on a load-bearing claim -- "every REST, GraphQL, and admin
call site routes through this single function" -- which nothing pinned. If a surface grew
its own creation path, or swallowed / reshaped the error on the way out, the coverage
suite would stay green while a real client saw a 500, a 200, or an unrecognisable body.

So this module drives the HTTP/admin surfaces themselves for the two resources Phase 6c
added, and asserts both the status and the *shared body*
(``OverLimitError.as_error_body()``: ``detail`` / ``code`` / ``resource`` /
``current_usage`` / ``limit`` / ``remedy``) -- REST as a 402 response body, GraphQL as the
same dict carried verbatim in the error's ``extensions``, admin as a non-500 error message.
"""

import datetime

from django.contrib.admin.sites import AdminSite
from django.contrib.messages import ERROR
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import (
    BillingState,
    Entitlement,
    LimitedResource,
    LimitKind,
    LimitRemedy,
)
from payments.models import (
    BillingPlan,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from public_api.admin import SystemUserAdmin
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


ERROR_BODY_KEYS = {"detail", "code", "resource", "current_usage", "limit", "remedy"}


def _container():
    from di_core.containers import container

    assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
    return container


def _organization_at_ceiling(
    resource_key: str, limit_value: int, *, can_invite_organizations: bool = False
) -> Organization:
    """An organization with a finite ceiling on ``resource_key`` and every entitlement
    granted (so the ``partner_api`` gate never masks the limit under test)."""
    organization = baker.make(
        Organization, parent=None, can_invite_organizations=can_invite_organizations
    )
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=resource_key,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    for entitlement_key in Entitlement.values:
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=entitlement_key,
            is_enabled=True,
        )
    return organization


def _admin_membership(organization: Organization) -> OrganizationMembership:
    from users.factories import UserFactory

    user = UserFactory().create_user()
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


def _assert_shared_error_body(body: dict, resource_key: str) -> None:
    assert set(body.keys()) == ERROR_BODY_KEYS, body
    assert body["code"] == "limit_exceeded"
    assert body["resource"] == resource_key
    assert body["remedy"] in set(LimitRemedy.values)
    assert body["detail"]


# ---------------------------------------------------------------------------
# webhook_subscriptions
# ---------------------------------------------------------------------------

CREATE_WEBHOOK_MUTATION = """
mutation CreateWebhookConfiguration($input: CreateWebhookConfigurationInput!) {
    createWebhookConfiguration(input: $input) {
        configuration { id }
        errorMessage
    }
}
"""


def _seed_webhook(organization: Organization) -> WebhookConfiguration:
    return baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/seed",
    )


@pytest.mark.django_db
class TestWebhookSubscriptionSurfaces:
    def test_rest_create_returns_the_shared_402(self):
        organization = _organization_at_ceiling(LimitedResource.WEBHOOK_SUBSCRIPTIONS, 1)
        _seed_webhook(organization)
        membership = _admin_membership(organization)

        client = APIClient()
        client.force_authenticate(user=membership.user)
        response = client.post(
            reverse("api:WebhookConfigurations-list"),
            {
                "event_type": WebhookEventType.CALENDAR_EVENT_CREATED,
                "url": "https://example.com/blocked",
                "headers": {},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        _assert_shared_error_body(response.json(), LimitedResource.WEBHOOK_SUBSCRIPTIONS)
        assert not WebhookConfiguration.objects.filter(
            organization=organization, url="https://example.com/blocked"
        ).exists()

    def test_graphql_create_carries_the_same_body_in_extensions(self):
        organization = _organization_at_ceiling(LimitedResource.WEBHOOK_SUBSCRIPTIONS, 1)
        _seed_webhook(organization)

        auth_service = _container().public_api_auth_service()
        system_user, token = auth_service.create_system_user(
            integration_name="webhook-surface-token",
            organization=organization,
            bypass_limits=True,
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.WEBHOOK_CONFIGURATION,
        )

        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_WEBHOOK_MUTATION,
                "variables": {
                    "input": {
                        "eventType": WebhookEventType.CALENDAR_EVENT_CREATED,
                        "url": "https://example.com/blocked-gql",
                        "headers": {},
                    }
                },
            },
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )

        # graphql-core always answers 200 and puts the failure in `errors`.
        assert response.status_code == status.HTTP_200_OK
        errors = response.json()["errors"]
        _assert_shared_error_body(errors[0]["extensions"], LimitedResource.WEBHOOK_SUBSCRIPTIONS)
        assert not WebhookConfiguration.objects.filter(
            organization=organization, url="https://example.com/blocked-gql"
        ).exists()


# ---------------------------------------------------------------------------
# public_api_system_users
# ---------------------------------------------------------------------------

CREATE_SYSTEM_USER_TOKEN_MUTATION = """
mutation CreateSystemUserToken($input: CreateSystemUserTokenInput!) {
    createSystemUserToken(input: $input) {
        token
    }
}
"""


@pytest.mark.django_db
class TestPublicApiSystemUserSurfaces:
    def test_rest_create_returns_the_shared_402(self):
        organization = _organization_at_ceiling(LimitedResource.PUBLIC_API_SYSTEM_USERS, 1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="surface-seed",
            long_lived_token_hash="surface-seed-hash",
        )
        membership = _admin_membership(organization)

        client = APIClient()
        client.force_authenticate(user=membership.user)
        response = client.post(
            reverse("api:PublicAPITokens-list"),
            {
                "integration_name": "surface-blocked",
                "available_resources": [PublicAPIResources.CALENDAR],
            },
            format="json",
        )

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        _assert_shared_error_body(response.json(), LimitedResource.PUBLIC_API_SYSTEM_USERS)
        assert not SystemUser.objects.filter(
            organization=organization, integration_name="surface-blocked"
        ).exists()

    def test_graphql_create_carries_the_same_body_in_extensions(self):
        """``createSystemUserToken`` is the reseller-facing minting path -- a *different*
        entry point from the REST serializer, routed through the same guarded service."""
        organization = _organization_at_ceiling(
            LimitedResource.PUBLIC_API_SYSTEM_USERS, 1, can_invite_organizations=True
        )
        auth_service = _container().public_api_auth_service()
        system_user, token = auth_service.create_system_user(
            integration_name="systemuser-surface-token",
            organization=organization,
            bypass_limits=True,
        )
        # `createSystemUserToken` maps to the SYSTEM_USER resource in
        # `public_api.permissions`; ORGANIZATION is what `assert_org_can_invite` reads.
        for resource_name in (
            PublicAPIResources.SYSTEM_USER,
            PublicAPIResources.ORGANIZATION,
            PublicAPIResources.CALENDAR,
        ):
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource_name)

        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_SYSTEM_USER_TOKEN_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": str(organization.id),
                        "integrationName": "surface-blocked-gql",
                        "resources": [PublicAPIResources.CALENDAR],
                    }
                },
            },
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )

        assert response.status_code == status.HTTP_200_OK
        errors = response.json()["errors"]
        _assert_shared_error_body(errors[0]["extensions"], LimitedResource.PUBLIC_API_SYSTEM_USERS)
        assert not SystemUser.objects.filter(
            organization=organization, integration_name="surface-blocked-gql"
        ).exists()

    def test_admin_create_reports_an_error_instead_of_a_500(self):
        """``SystemUserAdmin.save_model`` must not let ``OverLimitError`` escape.

        There is no HTTP status to assert here -- an escaping exception renders Django's
        500 debug page, so the observable contract is "``save_model`` returns, and the
        admin user is shown an ERROR message".
        """
        organization = _organization_at_ceiling(LimitedResource.PUBLIC_API_SYSTEM_USERS, 1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="admin-seed",
            long_lived_token_hash="admin-seed-hash",
        )

        model_admin = SystemUserAdmin(SystemUser, AdminSite())
        request = _admin_request()
        form = _fake_form({"integration_name": "admin-blocked", "organization": organization})

        model_admin.save_model(request, SystemUser(), form, change=False)

        assert not SystemUser.objects.filter(
            organization=organization, integration_name="admin-blocked"
        ).exists()
        level, message = request._messages_recorded[-1]
        assert level == ERROR
        assert "public api system user" in message.lower() or "limit" in message.lower()

    def test_admin_create_without_an_organization_still_works(self):
        """``SystemUser.organization`` is ``null=True`` and ``save_model`` branches
        explicitly on the org-less case ("access to all organizations"). The limit guard
        must skip it rather than resolve a billing root from ``None``, which raised
        ``AttributeError`` -> admin 500 on an explicitly supported path.
        """
        model_admin = SystemUserAdmin(SystemUser, AdminSite())
        request = _admin_request()
        form = _fake_form({"integration_name": "orgless-token", "organization": None})

        model_admin.save_model(request, SystemUser(), form, change=False)

        # `SystemUser.objects` refuses an unfiltered query on an OrganizationModel;
        # an org-less row has no organization to filter by, so this is the one legitimate
        # use of the unscoped manager here.
        created = SystemUser.original_manager.filter(integration_name="orgless-token").first()
        assert created is not None
        assert created.organization_id is None
        level, message = request._messages_recorded[-1]
        assert level != ERROR
        assert "all organizations" in message


# ---------------------------------------------------------------------------
# Admin test doubles
# ---------------------------------------------------------------------------


class _FakeForm:
    def __init__(self, cleaned_data):
        self.cleaned_data = cleaned_data


def _fake_form(cleaned_data):
    return _FakeForm(cleaned_data)


def _admin_request():
    """A request object that records ``message_user`` output.

    ``ModelAdmin.message_user`` goes through ``django.contrib.messages``, which needs a
    session + message storage a bare ``RequestFactory`` request does not have. Recording
    the calls directly keeps the test about ``save_model``'s behaviour rather than about
    the messages framework's plumbing.
    """
    from django.test import RequestFactory

    request = RequestFactory().post("/admin/public_api/systemuser/add/")
    request._messages_recorded = []

    class _Recorder:
        def add(self, level, message, extra_tags=""):
            request._messages_recorded.append((level, message))

    request._messages = _Recorder()
    return request

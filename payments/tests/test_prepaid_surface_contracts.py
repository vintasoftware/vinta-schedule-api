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
same dict carried verbatim in the error's ``extensions``, admin (driven through
``django.test.Client`` against the real ``admin:public_api_systemuser_add`` URL, not
``ModelAdmin.save_model`` called directly) as a 200 re-render of the add form with the
limit surfaced as a field error.
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import Client
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
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration


User = get_user_model()


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


def _admin_client() -> Client:
    superuser = User.objects.create_superuser(
        email="prepaid-surface-admin@example.com",
        password="adminpassword",  # noqa: S106
    )
    client = Client()
    client.force_login(superuser)
    return client


def _empty_resource_access_inline_formset_data() -> dict:
    """Management-form data for ``SystemUserAdmin``'s ``ResourceAccessInline``.

    Django admin's ``_changeform_view`` requires every inline's formset management
    form fields in the POST body, even when adding zero inline rows -- the prefix is
    ``ResourceAccess.system_user``'s ``related_name`` (``available_resources``), not
    the model name.
    """
    return {
        "available_resources-TOTAL_FORMS": "0",
        "available_resources-INITIAL_FORMS": "0",
        "available_resources-MIN_NUM_FORMS": "0",
        "available_resources-MAX_NUM_FORMS": "1000",
    }


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
        """The real ``admin:public_api_systemuser_add`` endpoint, over-limit.

        ``SystemUserAdminForm.clean()`` must reject the create with a field error
        *before* ``ModelAdmin._changeform_view`` ever reaches ``response_add`` --
        which would otherwise be called with ``obj.pk is None`` (the guarded
        ``save_model`` never ran) and 500 with ``NoReverseMatch`` reversing
        ``admin:public_api_systemuser_change`` for a ``None`` pk, instead of the 200
        field-error re-render asserted here.
        """
        organization = _organization_at_ceiling(LimitedResource.PUBLIC_API_SYSTEM_USERS, 1)
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="admin-seed",
            long_lived_token_hash="admin-seed-hash",
        )
        client = _admin_client()

        response = client.post(
            reverse("admin:public_api_systemuser_add"),
            data={
                "organization": str(organization.pk),
                "integration_name": "admin-blocked",
                **_empty_resource_access_inline_formset_data(),
            },
        )

        assert response.status_code == status.HTTP_200_OK
        assert not SystemUser.objects.filter(
            organization=organization, integration_name="admin-blocked"
        ).exists()
        content = response.content.decode()
        assert "public api system user" in content.lower() or "limit" in content.lower()

    def test_admin_create_without_an_organization_still_works(self):
        """``SystemUser.organization`` is ``null=True`` and ``save_model`` branches
        explicitly on the org-less case ("access to all organizations"). The limit guard
        must skip it rather than resolve a billing root from ``None``, which raised
        ``AttributeError`` -> admin 500 on an explicitly supported path.
        """
        client = _admin_client()

        response = client.post(
            reverse("admin:public_api_systemuser_add"),
            data={
                "organization": "",
                "integration_name": "orgless-token",
                **_empty_resource_access_inline_formset_data(),
            },
        )

        assert response.status_code == 302
        # `SystemUser.objects` refuses an unfiltered query on an OrganizationModel;
        # an org-less row has no organization to filter by, so this is the one legitimate
        # use of the unscoped manager here.
        created = SystemUser.original_manager.filter(integration_name="orgless-token").first()
        assert created is not None
        assert created.organization_id is None

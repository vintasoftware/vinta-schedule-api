import datetime
import json
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone

import pytest
from allauth.socialaccount.providers.base import ProviderException
from model_bakery import baker

from organizations.models import Organization, OrganizationBranding, OrganizationMembership
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement
from users.factories import UserFactory


class TestProviderCallbackAPIView:
    @staticmethod
    def get_url():
        return reverse("provider_callback_json")

    @pytest.mark.django_db
    def test_missing_provider_id(self, client):
        data = {"code": "dummy_code"}
        response = client.post(
            self.get_url(), data=json.dumps(data), content_type="application/json"
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Provider ID is required."

    @pytest.mark.django_db
    def test_error_in_data(self, client):
        data = {"provider_id": "test", "error": "some_error"}
        with patch("accounts.views.get_socialaccount_adapter") as mock_adapter:
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.login_cancelled_error = "cancelled"
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 400
        assert "error" in response.json()
        assert "message" in response.json()

    @pytest.mark.django_db
    def test_success(self, client):
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch("accounts.views.statekit.unstash_state", return_value={"next": "/callback/"}),
            patch("accounts.views.complete_social_login", return_value=JsonResponse({"ok": True})),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.return_value = MagicMock()
            mock_oauth2_adapter.complete_login.return_value = MagicMock()
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = "/callback/"
            mock_provider.app = mock_app
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    @pytest.mark.django_db
    def test_authentication_failed(self, client):
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch("accounts.views.statekit.unstash_state", return_value={"next": "/callback/"}),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.side_effect = ProviderException("fail")
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = "/callback/"
            mock_provider.app = mock_app
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 400
        assert "error" in response.json()
        assert "message" in response.json()


@pytest.mark.django_db
class TestProviderCallbackDestinationResolution:
    """Phase 7 -- the callback's response carries the resolved post-authentication
    destination, and no client-supplied value (``state["next"]``, which doubles as the
    OAuth ``callback_url``) can influence it.

    ``_complete_login`` mocks the whole social-login/token-exchange pipeline exactly
    like ``TestProviderCallbackAPIView`` above -- that machinery is allauth-headless
    internals and is not what this class is about. What makes ``request.user``
    authenticated here is ``client.force_login(user)``, called *before* the POST, so
    the view's own post-login destination resolution runs against a real,
    authenticated ``request.user`` while the surrounding OAuth exchange stays a stub.
    """

    @staticmethod
    def get_url():
        return reverse("provider_callback_json")

    def _complete_login(self, client, user, *, next_url="https://client.example/callback"):
        client.force_login(user)
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch(
                "accounts.views.statekit.unstash_state",
                return_value={"next": next_url, "process": "login"},
            ),
            patch(
                "accounts.views.complete_social_login",
                return_value=HttpResponseRedirect(next_url),
            ),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.return_value = MagicMock()
            mock_oauth2_adapter.complete_login.return_value = MagicMock()
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = next_url
            mock_provider.app = mock_app
            # The headless "app" client authenticates off an explicit
            # ``X-Session-Token`` header, not the session cookie -- see
            # ``allauth.headless.internal.authkit.authentication_context``, which
            # swaps ``request.session`` for a fresh, empty one and only restores the
            # cookie-backed session if this header resolves to it. ``force_login``
            # above still does the real work of building a legitimate,
            # database-backed session (with ``_auth_user_id`` etc. set); this just
            # forwards its key the way a real headless "app" client would.
            return client.post(
                self.get_url(),
                data=json.dumps(data),
                content_type="application/json",
                HTTP_X_SESSION_TOKEN=client.session.session_key,
            )

    def test_branded_entitled_organization_returns_configured_destination(self, client):
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == "https://org.example.com/app"

    def test_unbranded_organization_returns_dashboard(self, client):
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == f"{settings.FRONTEND_BASE_URL}/dashboard"

    @pytest.mark.no_auto_subscription
    def test_unentitled_organization_with_branding_row_returns_dashboard(self, client):
        user = UserFactory().create_user()
        org = baker.make(Organization)
        now = timezone.now()
        subscription = baker.make(
            Subscription,
            organization=org,
            plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
            billing_state=BillingState.FREE,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=False,
        )
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == f"{settings.FRONTEND_BASE_URL}/dashboard"

    def test_entitled_organization_with_no_redirect_url_returns_dashboard(self, client):
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(OrganizationBranding, organization=org, redirect_url="")

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == f"{settings.FRONTEND_BASE_URL}/dashboard"

    def test_reseller_child_membership_returns_reseller_destination(self, client):
        """Acceptance scenario 8 -- the redirect resolution applies to reseller
        subtrees under the unified route just like a standalone organization."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        baker.make(
            OrganizationBranding,
            organization=reseller,
            redirect_url="https://reseller.example.com/app",
        )
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)
        user = UserFactory().create_user()
        baker.make(OrganizationMembership, user=user, organization=child)

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == "https://reseller.example.com/app"

    def test_client_supplied_next_cannot_override_the_resolved_destination(self, client):
        """The open-redirect regression guard: a client-supplied ``state["next"]``
        (which doubles as the OAuth ``callback_url``) pointing somewhere else must
        never change the returned destination."""
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )

        response = self._complete_login(client, user, next_url="https://evil.example/steal")

        assert response.status_code == 200
        assert response.json()["destination"] == "https://org.example.com/app"
        assert response.json()["destination"] != "https://evil.example/steal"

    def test_client_supplied_next_cannot_override_the_dashboard_fallback(self, client):
        """Same guard, unbranded-organization side: an evil ``next`` must not leak
        through as the dashboard fallback either."""
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)

        response = self._complete_login(client, user, next_url="https://evil.example/steal")

        assert response.status_code == 200
        assert response.json()["destination"] == f"{settings.FRONTEND_BASE_URL}/dashboard"
        assert response.json()["destination"] != "https://evil.example/steal"


@pytest.mark.django_db
class TestGenericLoginPathUnaffectedByBrowserContext:
    """Phase 8 -- Branded login by organization slug.

    The organization-scoped login URL is resolved entirely on the SPA side through
    ``brandingForTenant(slug=...)`` (Phase 5): there is no backend routing to add for
    it, because the OAuth redirect/callback endpoints below take no
    organization/slug/tenant input and never acquire one from the request. This class
    pins that guarantee so it survives future refactors:

    - The generic login path (no organization in the URL) is unchanged: a cold visit
      still goes through ``ProviderRedirectAPIView`` with no organization concept at
      all, and ``ProviderCallbackAPIView`` still resolves the post-authentication
      destination purely from the authenticated user's own active membership
      (``get_active_organization_membership`` / Phase 7) -- never from the request.
    - Nothing about the destination changes when the request carries an
      ``X-Organization-Id`` header (the header ordinary tenant-scoped REST endpoints
      honor via ``TenantScopedViewMixin``), a ``Referer`` header naming a different
      organization's branded login URL, or an organization-flavored cookie. This view
      never mixes in that tenant-scoping machinery, so none of these are read.
    """

    @staticmethod
    def get_redirect_url():
        return reverse("provider_redirect_json")

    @staticmethod
    def get_callback_url():
        return reverse("provider_callback_json")

    def test_redirect_form_has_no_organization_or_slug_field(self):
        """The redirect-initiation form accepts only ``provider``, ``callback_url``,
        and ``process`` -- there is no organization/tenant/slug field for a client to
        supply in the first place, so there is nothing for the view to read even if a
        caller tried to smuggle one in."""
        from allauth.headless.socialaccount.forms import RedirectToProviderForm

        assert set(RedirectToProviderForm.base_fields) == {"provider", "callback_url", "process"}

    def _complete_login(
        self, client, user, *, next_url="https://client.example/callback", **extra_headers
    ):
        """Mirrors ``TestProviderCallbackDestinationResolution._complete_login``, with
        the addition of forwarding arbitrary extra request headers (``HTTP_*`` /
        ``HTTP_X_ORGANIZATION_ID`` / ``HTTP_REFERER``) so each test below can assert
        those headers have zero effect on the resolved destination.
        """
        client.force_login(user)
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch(
                "accounts.views.statekit.unstash_state",
                return_value={"next": next_url, "process": "login"},
            ),
            patch(
                "accounts.views.complete_social_login",
                return_value=HttpResponseRedirect(next_url),
            ),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.return_value = MagicMock()
            mock_oauth2_adapter.complete_login.return_value = MagicMock()
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = next_url
            mock_provider.app = mock_app
            return client.post(
                self.get_callback_url(),
                data=json.dumps(data),
                content_type="application/json",
                HTTP_X_SESSION_TOKEN=client.session.session_key,
                **extra_headers,
            )

    def test_callback_destination_ignores_x_organization_id_header(self, client):
        """A client-supplied ``X-Organization-Id`` naming a DIFFERENT organization
        than the user's real membership must not steer the resolved destination --
        this view never mixes in ``TenantScopedViewMixin``, so the header is never
        read on this path."""
        user = UserFactory().create_user()
        org = baker.make(Organization)
        other_org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )
        baker.make(
            OrganizationBranding,
            organization=other_org,
            redirect_url="https://other-org.example.com/app",
        )

        response = self._complete_login(client, user, HTTP_X_ORGANIZATION_ID=str(other_org.id))

        assert response.status_code == 200
        assert response.json()["destination"] == "https://org.example.com/app"

    def test_callback_destination_ignores_referer_header(self, client):
        """A ``Referer`` pointing at a different organization's branded login URL
        must not steer the resolved destination -- the destination is resolved only
        from the authenticated user's own membership, never from where the browser
        says it came from."""
        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )

        response = self._complete_login(
            client,
            user,
            HTTP_REFERER="https://app.example.com/login/some-other-org-slug/",
        )

        assert response.status_code == 200
        assert response.json()["destination"] == "https://org.example.com/app"

    def test_callback_destination_ignores_organization_cookie(self, client):
        """An organization-flavored cookie must not steer the resolved destination --
        there is no cookie-based organization inference on this path at all."""
        user = UserFactory().create_user()
        org = baker.make(Organization)
        other_org = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=org)
        baker.make(
            OrganizationBranding, organization=org, redirect_url="https://org.example.com/app"
        )
        baker.make(
            OrganizationBranding,
            organization=other_org,
            redirect_url="https://other-org.example.com/app",
        )
        client.cookies["organization_slug"] = "whatever-the-other-org-slug-is"

        response = self._complete_login(client, user)

        assert response.status_code == 200
        assert response.json()["destination"] == "https://org.example.com/app"


class TestProviderRedirectAPIView:
    @staticmethod
    def get_url():
        from django.urls import reverse

        return reverse("provider_redirect_json")

    @pytest.mark.django_db
    def test_invalid_form(self, client):
        # Missing required fields
        data = {}
        response = client.post(
            self.get_url(), data=json.dumps(data), content_type="application/json"
        )
        assert response.status_code == 400
        assert isinstance(response.json(), dict)
        assert any(k in response.json() for k in ["provider", "callback_url", "process"])

    @pytest.mark.django_db
    def test_success(self, client):
        # Patch all external/provider logic
        with (
            patch("accounts.views.RedirectToProviderForm") as mock_form,
            patch("accounts.views.get_socialaccount_adapter"),
        ):
            mock_form_instance = mock_form.return_value
            mock_form_instance.is_valid.return_value = True
            mock_provider = MagicMock()
            mock_app = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            # Setup cleaned_data
            mock_form_instance.cleaned_data = {
                "provider": mock_provider,
                "callback_url": "https://callback/",
                "process": "login",
            }
            mock_provider.app = mock_app
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_auth_params.return_value = {"foo": "bar"}
            mock_provider.get_pkce_params.return_value = {"code_verifier": "verifier"}
            mock_provider.get_scope.return_value = ["email"]
            mock_provider.stash_redirect_state.return_value = "stateid"
            mock_client.get_redirect_url.return_value = "https://provider/redirect"
            mock_oauth2_adapter.authorize_url = "https://provider/auth"

            # Simulate session
            class DummySession(dict):
                def save(self):
                    self["session_key"] = "sessiontoken"

                @property
                def session_key(self):
                    return self.get("session_key", "sessiontoken")

            # Patch request.session
            from django.test.client import RequestFactory

            rf = RequestFactory()
            request = rf.post(self.get_url(), data=json.dumps({}), content_type="application/json")
            request.session = DummySession()
            # Actually call the view
            from accounts.views import ProviderRedirectAPIView

            view = ProviderRedirectAPIView.as_view()
            response = view(request)
            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["redirect_url"] == "https://provider/redirect"
            assert data["session_token"] == "sessiontoken"

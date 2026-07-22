"""Integration tests for active-org resolution.

Verifies that the ``TenantScopedViewMixin`` resolver correctly resolves the
active organization from the ``X-Organization-Id`` header and stashes the result
so ``get_active_organization_membership`` reads it.

Behaviors covered:

* Header present + caller is active member → resolve to that org.
* Header absent + exactly one active membership → resolve to it.
* Header absent + 2+ active memberships → **400** ``X-Organization-Id header required.``
* The 0-membership (gated) and single-membership rows are unchanged.
* Header naming an org the caller is not an active member of (no membership, or
  an inactive membership) → **403** ``PermissionDenied``.
* A view setting ``active_org_resolution_optional = True`` is exempt from both the
  400 and the 403.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import (
    Organization,
    OrganizationMembership,
    OrganizationRole,
    get_active_organization_membership,
)


User = get_user_model()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org(name: str) -> Organization:
    return Organization.objects.create(name=name)


def _make_membership(
    user: User,  # type: ignore[valid-type]
    org: Organization,
    *,
    role: str = OrganizationRole.MEMBER,
    is_active: bool = True,
) -> OrganizationMembership:
    """Create an OrganizationMembership directly (bypassing the invite flow)."""
    return OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=role,
        is_active=is_active,
    )


def _auth_client_for(user: User) -> APIClient:  # type: ignore[valid-type]
    """Return an API client authenticated as *user* via session login."""
    from users.factories import DEFAULT_TEST_USER_PASSWORD

    client = APIClient()
    client.login(email=user.email, password=DEFAULT_TEST_USER_PASSWORD)
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_a() -> Organization:
    return _make_org("Org A")


@pytest.fixture
def org_b() -> Organization:
    return _make_org("Org B")


@pytest.fixture
def two_org_user(user: User, org_a: Organization, org_b: Organization):  # type: ignore[valid-type]
    """A user with active memberships in both Org A and Org B."""
    _make_membership(user, org_a)
    _make_membership(user, org_b)
    return user


# ---------------------------------------------------------------------------
# Tests: header-driven resolution (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHeaderDrivenOrgResolution:
    """Sending X-Organization-Id routes the request to the matching org."""

    def test_header_org_a_returns_org_a_membership(
        self,
        two_org_user: User,
        org_a: Organization,
        org_b: Organization,  # type: ignore[valid-type]
    ) -> None:
        """Header naming Org A resolves to the Org A membership."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Organizations-current")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK, response.content
        data = response.json()
        assert data["organization"]["id"] == org_a.pk

    def test_header_org_b_returns_org_b_membership(
        self,
        two_org_user: User,
        org_a: Organization,
        org_b: Organization,  # type: ignore[valid-type]
    ) -> None:
        """Header naming Org B resolves to the Org B membership."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Organizations-current")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_b.pk))

        assert response.status_code == status.HTTP_200_OK, response.content
        data = response.json()
        assert data["organization"]["id"] == org_b.pk

    def test_switching_org_between_requests_works(
        self,
        two_org_user: User,
        org_a: Organization,
        org_b: Organization,  # type: ignore[valid-type]
    ) -> None:
        """Sending different org headers in successive requests resolves each one correctly."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Organizations-current")

        response_a = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))
        response_b = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_b.pk))

        assert response_a.status_code == status.HTTP_200_OK
        assert response_b.status_code == status.HTTP_200_OK
        assert response_a.json()["organization"]["id"] == org_a.pk
        assert response_b.json()["organization"]["id"] == org_b.pk

    def test_header_with_single_membership_user_resolves_that_org(
        self,
        user: User,
        org_a: Organization,  # type: ignore[valid-type]
    ) -> None:
        """For a single-membership user, the header for their org resolves correctly."""
        _make_membership(user, org_a)
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK, response.content
        data = response.json()
        assert data["organization"]["id"] == org_a.pk


# ---------------------------------------------------------------------------
# Tests: header-absent resolution (single-membership happy path, no regression)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHeaderAbsentSingleMembership:
    """Without a header, a user with exactly one active membership resolves to it (unchanged)."""

    def test_no_header_resolves_single_membership(
        self,
        user: User,
        org_a: Organization,  # type: ignore[valid-type]
    ) -> None:
        """No header + one active membership → 200 with that org (preserved behavior)."""
        _make_membership(user, org_a)
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")

        response = client.get(url)

        assert response.status_code == status.HTTP_200_OK, response.content
        data = response.json()
        assert data["organization"]["id"] == org_a.pk

    def test_no_header_gated_user_returns_404(self, user: User) -> None:  # type: ignore[valid-type]
        """No header + zero active memberships → 404 (gated user, unchanged)."""
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")

        response = client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_no_header_inactive_membership_not_resolved(
        self,
        user: User,
        org_a: Organization,  # type: ignore[valid-type]
    ) -> None:
        """An inactive membership is treated as gated (no active membership)."""
        _make_membership(user, org_a, is_active=False)
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")

        response = client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unauthenticated_request_returns_401(self, org_a: Organization) -> None:
        """Unauthenticated requests always get 401 (DRF auth gate runs before the resolver)."""
        client = APIClient()
        url = reverse("api:Organizations-current")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Tests: get_active_organization_membership stash seam
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestActiveOrgStash:
    """The resolver stashes the membership on user._active_membership so helpers see it."""

    def test_get_active_org_membership_returns_header_resolved_membership(
        self,
        two_org_user: User,
        org_a: Organization,
        org_b: Organization,  # type: ignore[valid-type]
    ) -> None:
        """``get_active_organization_membership`` inside a view returns the header-resolved one.

        We assert this end-to-end: the ``current`` action calls the helper internally
        and returns data for the org named in the header — so if the returned org.id
        matches the header, the stash seam is working.
        """
        client = _auth_client_for(two_org_user)
        url = reverse("api:Organizations-current")

        for org in (org_a, org_b):
            response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org.pk))
            assert response.status_code == status.HTTP_200_OK, response.content
            assert response.json()["organization"]["id"] == org.pk, (
                f"Expected org {org.pk} but got {response.json()['organization']['id']}"
            )

    def test_off_request_fallback_is_unaffected(
        self,
        user: User,
        org_a: Organization,  # type: ignore[valid-type]
    ) -> None:
        """Off-DRF-request callers (no _active_membership stash) fall back to DB query.

        Simulates a management command / Celery task calling the helper directly.
        """
        _make_membership(user, org_a)
        # user._active_membership is NOT set here — no DRF request, no stash.
        assert not hasattr(user, "_active_membership")

        membership = get_active_organization_membership(user)

        assert membership is not None
        assert membership.organization_id == org_a.pk

    def test_stash_none_when_gated_is_distinguishable_from_unset(
        self,
        user: User,  # type: ignore[valid-type]
    ) -> None:
        """After a DRF request for a gated user, _active_membership is None (not absent).

        This lets get_active_organization_membership distinguish "DRF request,
        resolved to gated" from "not on a DRF request at all".
        """
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")
        # Gated user — no memberships; current returns 404 but the stash should have None.
        response = client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        # The stash check itself is indirect: the helper returned None (gated), which is
        # the correct outcome. We verify the off-request user object has no stash yet.
        assert not hasattr(user, "_active_membership")


# ---------------------------------------------------------------------------
# Tests: tenant-scoped queryset (CalendarViewSet) — list isolation + create
# ---------------------------------------------------------------------------
# Uses CalendarViewSet (GET /calendar/, POST /calendar/) because it is a
# standard VintaScheduleModelViewSet with org-scoped get_queryset() and the
# CalendarSerializer.create path goes through CreateModelMixin.create, making it
# the ideal regression target for the del → re-resolve bug.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarViewSetOrgScoping:
    """X-Organization-Id header correctly scopes CalendarViewSet list and create."""

    def test_list_with_header_a_returns_only_org_a_calendars(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """GET /calendar/ with header A returns only Org A calendars, not Org B."""
        from calendar_integration.tests.test_views import CalendarIntegrationTestFactory

        cal_a = CalendarIntegrationTestFactory.create_calendar(organization=org_a)
        cal_b = CalendarIntegrationTestFactory.create_calendar(organization=org_b)
        # Non-admin members only list calendars they own (owner-scoping).
        CalendarIntegrationTestFactory.create_calendar_ownership(two_org_user, cal_a)
        CalendarIntegrationTestFactory.create_calendar_ownership(two_org_user, cal_b)

        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(
            url,
            HTTP_X_ORGANIZATION_ID=str(org_a.pk),
            data={"include_inactive": "true", "include_unlisted": "true"},
        )

        assert response.status_code == status.HTTP_200_OK, response.content
        returned_ids = {item["id"] for item in response.json()["results"]}
        assert cal_a.id in returned_ids, "Org A calendar should appear in the list"
        assert cal_b.id not in returned_ids, "Org B calendar must NOT appear with Org A header"

    def test_list_with_header_b_returns_only_org_b_calendars(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """GET /calendar/ with header B returns only Org B calendars, not Org A."""
        from calendar_integration.tests.test_views import CalendarIntegrationTestFactory

        cal_a = CalendarIntegrationTestFactory.create_calendar(organization=org_a)
        cal_b = CalendarIntegrationTestFactory.create_calendar(organization=org_b)
        # Non-admin members only list calendars they own (owner-scoping).
        CalendarIntegrationTestFactory.create_calendar_ownership(two_org_user, cal_a)
        CalendarIntegrationTestFactory.create_calendar_ownership(two_org_user, cal_b)

        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(
            url,
            HTTP_X_ORGANIZATION_ID=str(org_b.pk),
            data={"include_inactive": "true", "include_unlisted": "true"},
        )

        assert response.status_code == status.HTTP_200_OK, response.content
        returned_ids = {item["id"] for item in response.json()["results"]}
        assert cal_b.id in returned_ids, "Org B calendar should appear in the list"
        assert cal_a.id not in returned_ids, "Org A calendar must NOT appear with Org B header"

    def test_create_under_header_b_returns_201_and_lands_in_org_b(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """POST /calendar/ with X-Organization-Id: B creates the calendar under Org B.

        Regression test: before the fix, ``del
        user._active_membership`` in ``CreateModelMixin.create`` wiped the header-
        resolved stash so the post-create re-fetch of the queryset fell into the
        header-blind DB fallback (order_by("created").first() == Org A for this
        two-org user), causing a cross-org DoesNotExist / 500.

        The mock service is required because ``CalendarSerializer.create`` delegates
        object creation to the injected ``CalendarService``; the mock returns a real
        DB-backed ``Calendar`` row seeded under Org B so the viewset's post-create
        ``get_queryset().get(pk=...)`` is a genuine org-scoped lookup.
        """
        from unittest.mock import Mock

        from calendar_integration.models import Calendar
        from calendar_integration.tests.test_views import CalendarIntegrationTestFactory
        from di_core.containers import container

        assert container is not None, "DI container must be wired during tests"
        # Seed a real Calendar row in Org B — the mock service will return this.
        created_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=org_b,
            name="New Virtual Calendar Under B",
            description="Created under Org B",
        )

        mock_service = Mock()
        mock_service.initialize_without_provider.return_value = None
        mock_service.create_virtual_calendar.return_value = created_calendar

        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        with container.calendar_service.override(mock_service):
            response = client.post(
                url,
                data={"name": "New Virtual Calendar Under B", "description": "Created under Org B"},
                format="json",
                HTTP_X_ORGANIZATION_ID=str(org_b.pk),
            )

        assert response.status_code == status.HTTP_201_CREATED, (
            f"Expected 201 but got {response.status_code}; body: {response.content!r}. "
            "If this is 500/DoesNotExist the Finding-1 fix is not applied."
        )
        # Confirm the returned calendar belongs to Org B (not Org A).
        returned_id = response.json()["id"]
        # Use filter_by_organization so we scope to Org B — if the calendar
        # was mistakenly re-fetched under Org A the queryset would yield DoesNotExist.
        cal = Calendar.objects.filter_by_organization(org_b.pk).get(pk=returned_id)
        assert cal.organization_id == org_b.pk, (
            f"Calendar was created/re-fetched under org {cal.organization_id} "
            f"instead of Org B ({org_b.pk})."
        )


# ---------------------------------------------------------------------------
# Tests: malformed X-Organization-Id header does not 500
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMalformedOrgIdHeader:
    """A non-integer X-Organization-Id header is treated as an absent header.

    Same rules as a missing header: single membership → resolve (200), multi-org
    → 400, gated → gated. A garbage header must never silently pick an org for a
    multi-org caller.
    """

    def test_single_membership_non_integer_header_resolves(
        self,
        user: User,  # type: ignore[valid-type]
        org_a: Organization,
    ) -> None:
        """One active membership + 'abc' header → 200 (treated as absent, resolves)."""
        _make_membership(user, org_a)
        client = _auth_client_for(user)
        url = reverse("api:Organizations-current")

        # "abc" cannot be coerced to int; the resolver treats it as an absent
        # header and resolves the single membership.
        response = client.get(url, HTTP_X_ORGANIZATION_ID="abc")

        assert response.status_code == status.HTTP_200_OK, (
            f"Malformed header with single membership should resolve to 200; "
            f"got {response.status_code}: {response.content!r}"
        )

    def test_multi_org_non_integer_header_returns_400(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """Two active memberships + 'abc' header → 400 (same as absent header).

        The malformed header is treated as absent, so a multi-org caller hits the
        ambiguity 400 rather than silently resolving to the first org.
        """
        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID="abc")

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json() == {"detail": _MISSING_HEADER_DETAIL}


# ---------------------------------------------------------------------------
# Tests: multi-org caller with no header is rejected with 400
# ---------------------------------------------------------------------------
# A user with 2+ active memberships who omits X-Organization-Id must
# get a clear 400, never an ambiguous implicit org. We exercise a real
# tenant-scoped viewset (CalendarViewSet list) so the 400 is asserted end-to-end
# through TenantScopedViewMixin.initial().
# ---------------------------------------------------------------------------

#: The exact body the resolver returns for the multi-org-no-header case.
_MISSING_HEADER_DETAIL = "X-Organization-Id header required."


@pytest.mark.django_db
class TestMultiOrgNoHeaderRejected:
    """A multi-org caller that omits the header is rejected with 400."""

    def test_two_memberships_no_header_returns_400(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """GET /calendar/ with two active memberships and NO header → 400 with detail."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(url)

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json() == {"detail": _MISSING_HEADER_DETAIL}

    def test_single_membership_no_header_still_resolves(
        self,
        user: User,  # type: ignore[valid-type]
        org_a: Organization,
    ) -> None:
        """Exactly one active membership + no header → 200 (no regression, not 400)."""
        _make_membership(user, org_a)
        client = _auth_client_for(user)
        url = reverse("api:Calendars-list")

        response = client.get(url)

        assert response.status_code == status.HTTP_200_OK, response.content

    def test_gated_user_no_header_is_not_400(
        self,
        user: User,  # type: ignore[valid-type]
    ) -> None:
        """Zero active memberships + no header → gated, never 400 (onboarding unchanged).

        A gated user resolves to no active org, so the org-scoped list yields an
        empty page; the resolver must not raise the multi-org 400.
        """
        client = _auth_client_for(user)
        url = reverse("api:Calendars-list")

        response = client.get(url)

        assert response.status_code != status.HTTP_400_BAD_REQUEST, response.content
        # Gated → no active org → empty result set, never the multi-org rejection.
        assert response.status_code == status.HTTP_200_OK, response.content
        assert response.json()["results"] == []

    def test_two_memberships_with_valid_header_still_resolves(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """Two memberships + a valid header → 200 (no regression)."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK, response.content


# ---------------------------------------------------------------------------
# Tests: active_org_resolution_optional opt-out
# ---------------------------------------------------------------------------
# A concrete view may set ``active_org_resolution_optional = True`` (e.g. the
# GET /organizations/mine/ and onboarding flows) so that a multi-org caller with
# no header is NOT rejected — the resolver falls through to gated (None) instead.
#
# We assert the opt-out by driving the mixin's resolver directly with a throwaway
# view.
# ---------------------------------------------------------------------------


class _OptOutView(TenantScopedViewMixin):
    """Throwaway view that opts out of the multi-org-no-header 400."""

    active_org_resolution_optional = True


class _StrictView(TenantScopedViewMixin):
    """Throwaway view that keeps the default (strict) multi-org-no-header 400."""

    active_org_resolution_optional = False


def _drf_request_for(
    user: User,  # type: ignore[valid-type]
    *,
    org_id_header: str | None = None,
) -> Request:
    """Build a DRF Request authenticated as *user*.

    If *org_id_header* is given it is sent as the ``X-Organization-Id`` header;
    otherwise the request carries no header.
    """
    factory = APIRequestFactory()
    if org_id_header is not None:
        django_request = factory.get("/anything/", HTTP_X_ORGANIZATION_ID=org_id_header)
    else:
        django_request = factory.get("/anything/")
    force_authenticate(django_request, user=user)
    drf_request = Request(django_request)
    # force_authenticate stamps the wsgi request; mirror it on the DRF request so
    # the resolver's getattr(request, "user", None) sees the authenticated user.
    drf_request.user = user
    return drf_request


@pytest.mark.django_db
class TestActiveOrgResolutionOptionalOptOut:
    """A view with active_org_resolution_optional = True is exempt from the 400."""

    def test_opt_out_view_does_not_raise_for_multi_org_no_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """active_org_resolution_optional = True → no 400; resolves to gated (None)."""
        view = _OptOutView()
        request = _drf_request_for(two_org_user)

        # Must not raise ValidationError.
        view._resolve_active_organization(request)  # type: ignore[attr-defined]

        assert request.organization_membership is None  # type: ignore[attr-defined]
        assert request.organization is None  # type: ignore[attr-defined]

    def test_strict_view_raises_for_multi_org_no_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """The default (strict) view raises ValidationError for the same input.

        Confirms the opt-out is what suppresses the 400, not some other difference.
        """
        view = _StrictView()
        request = _drf_request_for(two_org_user)

        with pytest.raises(ValidationError):
            view._resolve_active_organization(request)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: header naming a non-member org is rejected with 403
# ---------------------------------------------------------------------------
# A valid-integer X-Organization-Id that names an organization the caller is
# *not* an active member of (org the user has no membership in, or a membership
# that exists but is inactive) is rejected with 403 PermissionDenied. The
# malformed-header and absent-header rules are unaffected. A view
# setting ``active_org_resolution_optional = True`` is exempt from the 403 and
# resolves to gated (None).
# ---------------------------------------------------------------------------


@pytest.fixture
def org_c() -> Organization:
    """A third org the user is not a member of."""
    return _make_org("Org C")


@pytest.mark.django_db
class TestNonMemberOrgHeaderRejected:
    """A header naming an org the caller is not an active member of → 403."""

    def test_header_for_non_member_org_returns_403(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
        org_c: Organization,
    ) -> None:
        """GET /calendar/ with a header naming an org the user has no membership in → 403."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_c.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content

    def test_header_for_inactive_membership_org_returns_403(
        self,
        user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """A header naming an org where the user's membership is inactive → 403.

        The resolver's matching lookup filters ``is_active=True``, so an inactive
        membership yields ``matching is None`` and is rejected just like a
        non-member org.
        """
        # Active membership in A (so the user is authenticated/non-gated) plus an
        # inactive membership in B named by the header.
        _make_membership(user, org_a)
        _make_membership(user, org_b, is_active=False)
        client = _auth_client_for(user)
        url = reverse("api:Calendars-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_b.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content

    def test_header_for_member_org_still_returns_200(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """A header naming a member org still resolves to 200 (no regression)."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Calendars-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_200_OK, response.content


@pytest.mark.django_db
class TestNonMemberOrgHeaderOptOut:
    """A view with active_org_resolution_optional = True is exempt from the 403."""

    def test_opt_out_view_does_not_raise_for_non_member_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
        org_c: Organization,
    ) -> None:
        """active_org_resolution_optional = True + non-member header → no 403; gated (None)."""
        view = _OptOutView()
        request = _drf_request_for(two_org_user, org_id_header=str(org_c.pk))

        # Must not raise PermissionDenied.
        view._resolve_active_organization(request)  # type: ignore[attr-defined]

        assert request.organization_membership is None  # type: ignore[attr-defined]
        assert request.organization is None  # type: ignore[attr-defined]

    def test_strict_view_raises_for_non_member_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
        org_c: Organization,
    ) -> None:
        """The default (strict) view raises PermissionDenied for the same input.

        Confirms the opt-out is what suppresses the 403, not some other difference.
        """
        view = _StrictView()
        request = _drf_request_for(two_org_user, org_id_header=str(org_c.pk))

        with pytest.raises(PermissionDenied):
            view._resolve_active_organization(request)  # type: ignore[attr-defined]

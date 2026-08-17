"""Integration tests for active-org resolution.

Verifies that the ``TenantScopedViewMixin`` resolver correctly resolves the
active organization from the ``X-Organization-Id`` header and exposes the
package-selected membership on the request.

Behaviors covered:

* Header present + caller is active member → resolve to that org.
* Header absent + exactly one active membership → resolve to it.
* Header absent + 2+ active memberships → **400** ``X-Organization-Id header required.``
* The 0-membership (gated) and single-membership rows are unchanged.
* Header naming an org the caller is not an active member of (no membership, or
  an inactive membership) → **403** ``PermissionDenied``.
* A view setting ``organization_resolution_optional = True`` is exempt from both the
  400 and the 403.
"""

import ast
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from vinta_orgs.exceptions import AmbiguousOrganizationError

from common.organization_services import memberships
from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import (
    Organization,
    OrganizationMembership,
    OrganizationRole,
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
# Tests: package-owned membership resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPackageOwnedMembershipResolution:
    """Request and off-request callers both use the package's resolution contract."""

    def test_request_path_returns_header_resolved_membership(
        self,
        two_org_user: User,
        org_a: Organization,
        org_b: Organization,  # type: ignore[valid-type]
    ) -> None:
        """The current action reads the membership the package put on the request."""
        client = _auth_client_for(two_org_user)
        url = reverse("api:Organizations-current")

        for org in (org_a, org_b):
            response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org.pk))
            assert response.status_code == status.HTTP_200_OK, response.content
            assert response.json()["organization"]["id"] == org.pk, (
                f"Expected org {org.pk} but got {response.json()['organization']['id']}"
            )

    def test_off_request_single_membership_resolves_directly_with_the_package(
        self,
        user: User,
        org_a: Organization,  # type: ignore[valid-type]
    ) -> None:
        """A management command or task may resolve a single membership directly."""
        _make_membership(user, org_a)
        membership = memberships.resolve_for_user(user)

        assert membership is not None
        assert membership.organization_id == org_a.pk

    def test_off_request_two_memberships_are_ambiguous(
        self,
        two_org_user: User,  # type: ignore[valid-type]
    ) -> None:
        """Without an explicit organization, row age no longer selects a tenant."""
        with pytest.raises(AmbiguousOrganizationError):
            memberships.resolve_for_user(two_org_user)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATIC_GUARD_PRUNED_DIRS = frozenset({".git", ".venv", "migrations", "node_modules"})
STATIC_GUARD_SENTINELS = frozenset(
    {
        "organizations/models.py",
        "common/utils/view_utils.py",
        "organizations/tests/test_org_resolution.py",
    }
)


def test_application_has_no_legacy_membership_resolver_or_user_stash() -> None:
    """Keep membership resolution on the package request/direct-call seams."""
    removed_resolver = "get_active_" + "organization_membership"
    removed_user_attribute = "_" + "active_membership"
    scanned_paths: set[str] = set()
    violations: list[str] = []

    for directory, directory_names, file_names in os.walk(REPOSITORY_ROOT):
        directory_names[:] = [
            name for name in directory_names if name not in STATIC_GUARD_PRUNED_DIRS
        ]
        for file_name in file_names:
            if not file_name.endswith(".py"):
                continue
            path = Path(directory, file_name)
            relative_path = str(path.relative_to(REPOSITORY_ROOT))
            scanned_paths.add(relative_path)
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name == removed_resolver
                ):
                    violations.append(f"{relative_path}:{node.lineno}: definition")
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == removed_resolver for alias in node.names
                ):
                    violations.append(f"{relative_path}:{node.lineno}: import")
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name | ast.Attribute) and (
                        (isinstance(node.func, ast.Name) and node.func.id == removed_resolver)
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == removed_resolver
                        )
                    ):
                        violations.append(f"{relative_path}:{node.lineno}: call")
                    if (
                        isinstance(node.func, ast.Name | ast.Attribute)
                        and (
                            (
                                isinstance(node.func, ast.Name)
                                and node.func.id in {"getattr", "setattr", "delattr"}
                            )
                            or (
                                isinstance(node.func, ast.Attribute)
                                and node.func.attr in {"getattr", "setattr", "delattr"}
                            )
                        )
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in {removed_resolver, removed_user_attribute}
                    ):
                        violations.append(
                            f"{relative_path}:{node.lineno}: dynamic attribute access"
                        )
                if isinstance(node, ast.Attribute) and node.attr in {
                    removed_resolver,
                    removed_user_attribute,
                }:
                    violations.append(f"{relative_path}:{node.lineno}: attribute access")

    missing_sentinels = sorted(STATIC_GUARD_SENTINELS - scanned_paths)
    assert not missing_sentinels, (
        "The legacy-membership static guard did not reach known production and test modules; "
        f"scanned {len(scanned_paths)} files, missing {missing_sentinels}."
    )
    assert violations == []


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

        Regression test: before the fix, post-create resolution could discard
        the header-selected membership and fall back to Org A for this two-org
        user, causing a cross-org DoesNotExist / 500.

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
# Tests: organization_resolution_optional opt-out
# ---------------------------------------------------------------------------
# A concrete view may set ``organization_resolution_optional = True`` (e.g. the
# GET /organizations/mine/ and onboarding flows) so that a multi-org caller with
# no header is NOT rejected — the resolver falls through to gated (None) instead.
#
# We assert the opt-out by driving the mixin's resolver directly with a throwaway
# view.
# ---------------------------------------------------------------------------


class _OptOutView(TenantScopedViewMixin):
    """Throwaway view that opts out of the multi-org-no-header 400."""

    organization_resolution_optional = True


class _StrictView(TenantScopedViewMixin):
    """Throwaway view that keeps the default (strict) multi-org-no-header 400."""

    organization_resolution_optional = False


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
    """A view with organization_resolution_optional = True is exempt from the 400."""

    def test_opt_out_view_does_not_raise_for_multi_org_no_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
    ) -> None:
        """organization_resolution_optional = True → no 400; resolves to gated (None)."""
        view = _OptOutView()
        request = _drf_request_for(two_org_user)

        # Must not raise ValidationError.
        view.resolve_organization(request)  # type: ignore[attr-defined]

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
            view.resolve_organization(request)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: header naming a non-member org is rejected with 403
# ---------------------------------------------------------------------------
# A valid-integer X-Organization-Id that names an organization the caller is
# *not* an active member of (org the user has no membership in, or a membership
# that exists but is inactive) is rejected with 403 PermissionDenied. The
# malformed-header and absent-header rules are unaffected. A view
# setting ``organization_resolution_optional = True`` is exempt from the 403 and
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
    """A view with organization_resolution_optional = True is exempt from the 403."""

    def test_opt_out_view_does_not_raise_for_non_member_header(
        self,
        two_org_user: User,  # type: ignore[valid-type]
        org_a: Organization,
        org_b: Organization,
        org_c: Organization,
    ) -> None:
        """organization_resolution_optional = True + non-member header → no 403; gated (None)."""
        view = _OptOutView()
        request = _drf_request_for(two_org_user, org_id_header=str(org_c.pk))

        # Must not raise PermissionDenied.
        view.resolve_organization(request)  # type: ignore[attr-defined]

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
            view.resolve_organization(request)  # type: ignore[attr-defined]


@pytest.mark.django_db
class TestADeactivatedAdminIsRefusedThroughTheRealStack:
    """Deactivating an admin actually revokes their access, end to end.

    Phase 3.5's Tests item 3, asserted the way that item asks for it: a real
    request, a real routed admin-gated endpoint (``GET /organization-members/``,
    ``OrganizationMembershipViewSet``, ``permission_classes =
    (IsOrganizationAdmin,)``), the caller's own organization named in
    ``X-Organization-Id``. Not a unit test of the backend, and not a probe view.

    **Which gate answers, exactly.** The 403 below comes from the *resolver* --
    the package's membership queryset filters
    ``is_active=True``, so the header names an organization with no active
    membership behind it and ``memberships.resolve_for_user`` raises
    ``OrganizationAccessDeniedError`` before ``IsOrganizationAdmin`` is ever
    consulted. It does **not** come from the package's ``OrganizationModelBackend
    ._get_membership``, the other place ``0.3.0`` put an ``is_active`` filter:
    nothing in this repository calls ``user.has_perm(...)`` for authorization
    yet, so that filter is unreachable from any real request until Phase 4
    migrates the permission classes onto ``has_perm``. Covering it through our
    stack is recorded as a Phase 4 obligation in
    ``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``;
    ``organizations/tests/test_permission_backend.py`` unit-tests it meanwhile.

    Both tests below move exactly one field and assert the status code follows,
    so neither can pass for a reason unrelated to the field it names.
    """

    def test_deactivating_an_admin_flips_the_same_request_from_200_to_403(
        self,
        user: User,  # type: ignore[valid-type]
        org_a: Organization,
    ) -> None:
        """Same user, same client, same URL, same header -- only ``is_active`` moves."""
        membership = _make_membership(user, org_a, role=OrganizationRole.ADMIN)
        client = _auth_client_for(user)
        url = reverse("api:OrganizationMembers-list")

        allowed = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))
        assert allowed.status_code == status.HTTP_200_OK, allowed.content

        membership.is_active = False
        membership.save(update_fields=["is_active"])

        refused = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.content
        # The resolver's wording, not ``IsOrganizationAdmin``'s -- proof that the
        # deactivated row was refused at resolution rather than admitted to the
        # permission class and turned down there for some other reason.
        assert refused.json() == {
            "detail": (
                "X-Organization-Id header names an organization you are not an active member of."
            )
        }

    def test_an_active_non_admin_is_refused_by_the_permission_class_instead(
        self,
        user: User,  # type: ignore[valid-type]
        org_a: Organization,
    ) -> None:
        """The discriminator between the two ways this endpoint says 403.

        Without it, the test above would pass on a build where ``is_active`` were
        ignored and ``role`` alone did all the refusing. Here the membership is
        active and the *role* is what is wrong, so resolution succeeds and
        ``IsOrganizationAdmin`` answers -- a different body for the same code.
        """
        _make_membership(user, org_a, role=OrganizationRole.MEMBER)
        client = _auth_client_for(user)
        url = reverse("api:OrganizationMembers-list")

        response = client.get(url, HTTP_X_ORGANIZATION_ID=str(org_a.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert response.json() != {
            "detail": (
                "X-Organization-Id header names an organization you are not an active member of."
            )
        }

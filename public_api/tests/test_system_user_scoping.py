"""``PublicApiSystemUserMiddleware``: what it may read, and what it binds.

The public GraphQL API does not go through ``TenantScopedViewMixin`` -- Strawberry's
view is not a DRF view -- so this middleware is the only thing that binds an
organization on that path. It also runs in front of *every* request, including
the DRF ones, which makes its ordering load-bearing in two directions:

* **Before the binding** it resolves the credential. Everything it touches there
  has to be readable with no organization bound: ``SystemUser`` through
  ``original_manager`` (the token's id is globally unique and the organization is
  not known yet -- it is about to be read *off* the row), and ``Organization``,
  which is the tenant root rather than tenant-scoped data. A scoped read there
  would raise under ``STRICT_ORGANIZATION_FILTER`` and 500 the request.
* **After the binding** everything else runs, including the ``partner_api``
  entitlement gate and the whole GraphQL execution.

And it must leave nothing behind: the middleware wraps *every* request, so a
leaked binding here would be inherited by whatever the worker serves next.
"""

from typing import Any

from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory

import pytest
from model_bakery import baker
from vinta_orgs.exceptions import OrganizationNotFoundError

from common.organization_context import get_current_organization, organization_context
from organizations.models import Organization
from public_api.middlewares import PublicApiSystemUserMiddleware
from public_api.models import SystemUser


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Scoping Org")


@pytest.fixture
def other_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Other Scoping Org")


def _auth_service() -> Any:
    from di_core.containers import container

    assert container is not None  # noqa: S101 -- the DI container is wired by AppConfig.ready
    return container.public_api_auth_service()


def _middleware(recorder: list[Any]) -> PublicApiSystemUserMiddleware:
    """A middleware whose ``get_response`` records the binding it was called under."""

    def get_response(request: Any) -> HttpResponse:
        organization = get_current_organization()
        recorder.append(None if organization is None else organization.pk)
        return HttpResponse("ok")

    return PublicApiSystemUserMiddleware(get_response)


def _graphql_request(token_header: str | None = None, **extra: Any) -> Any:
    if token_header:
        extra["HTTP_AUTHORIZATION"] = token_header
    return RequestFactory().post("/graphql/", data="{}", content_type="application/json", **extra)


@pytest.mark.django_db
class TestTheMiddlewareBindsWhatItResolved:
    def test_an_authenticated_system_user_binds_its_own_organization(
        self, organization: Organization
    ) -> None:
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-bound", organization=organization, bypass_limits=True
        )
        seen: list[Any] = []

        response = _middleware(seen)(_graphql_request(f"Bearer {system_user.id}:{token}"))

        assert response.status_code == 200
        assert seen == [organization.pk]

    def test_an_org_less_token_binds_the_organization_the_header_names(
        self, organization: Organization
    ) -> None:
        """``X-Public-Api-Organization-Id`` is how a global credential picks a tenant."""
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-orgless", organization=None, bypass_limits=True
        )
        seen: list[Any] = []

        response = _middleware(seen)(
            _graphql_request(
                f"Bearer {system_user.id}:{token}",
                HTTP_X_PUBLIC_API_ORGANIZATION_ID=str(organization.pk),
            )
        )

        assert response.status_code == 200
        assert seen == [organization.pk]

    def test_an_anonymous_graphql_request_binds_nothing(self, db: Any) -> None:
        """``brandingForTenant`` and friends are unauthenticated; they get no tenant."""
        seen: list[Any] = []

        response = _middleware(seen)(_graphql_request())

        assert response.status_code == 200
        assert seen == [None]

    def test_a_non_graphql_request_binds_nothing(self, db: Any) -> None:
        seen: list[Any] = []

        response = _middleware(seen)(RequestFactory().get("/organizations/current/"))

        assert response.status_code == 200
        assert seen == [None]


@pytest.mark.django_db
class TestTheCredentialLookupRunsBeforeTheBinding:
    """The reads the middleware performs unbound must be ones that *can* run unbound."""

    def test_resolving_a_token_reads_the_row_the_scoped_manager_cannot_see(
        self, organization: Organization
    ) -> None:
        """``check_system_user_token`` succeeds with nothing bound.

        The companion assertion is what makes it meaningful: the same row is
        unreachable through the scoped manager at that moment, so resolving it
        through ``original_manager`` is not an over-cautious choice -- it is the
        only one that works.
        """
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-lookup", organization=organization, bypass_limits=True
        )

        assert get_current_organization() is None
        resolved, authenticated = _auth_service().check_system_user_token(system_user.id, token)
        assert authenticated is True
        assert resolved.pk == system_user.pk

        with pytest.raises(OrganizationNotFoundError):
            SystemUser.objects.get(pk=system_user.pk)

    def test_an_org_less_token_is_invisible_to_the_scoped_manager_even_when_bound(
        self, organization: Organization
    ) -> None:
        system_user, _token = _auth_service().create_system_user(
            integration_name="scoping-invisible", organization=None, bypass_limits=True
        )

        with organization_context(organization):
            assert not SystemUser.objects.filter(pk=system_user.pk).exists()

        assert SystemUser.original_manager.filter(pk=system_user.pk).exists()


@pytest.mark.django_db
class TestNothingSurvivesTheResponse:
    def test_a_normal_response_leaves_nothing_bound(self, organization: Organization) -> None:
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-teardown", organization=organization, bypass_limits=True
        )

        _middleware([])(_graphql_request(f"Bearer {system_user.id}:{token}"))

        assert get_current_organization() is None

    def test_an_exception_from_the_inner_view_leaves_nothing_bound(
        self, organization: Organization
    ) -> None:
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-teardown-raise",
            organization=organization,
            bypass_limits=True,
        )

        def boom(request: Any) -> HttpResponse:
            assert get_current_organization() is not None  # noqa: S101 -- guards the test
            raise RuntimeError("boom")

        middleware = PublicApiSystemUserMiddleware(boom)

        with pytest.raises(RuntimeError, match="boom"):
            middleware(_graphql_request(f"Bearer {system_user.id}:{token}"))

        assert get_current_organization() is None

    def test_the_402_short_circuit_leaves_nothing_bound(
        self, organization: Organization, monkeypatch: Any
    ) -> None:
        """The entitlement gate returns *from inside* the bound block."""
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-teardown-402",
            organization=organization,
            bypass_limits=True,
        )
        monkeypatch.setattr(
            PublicApiSystemUserMiddleware,
            "_has_partner_api_entitlement",
            lambda self, organization: False,
        )

        response = _middleware([])(_graphql_request(f"Bearer {system_user.id}:{token}"))

        assert isinstance(response, JsonResponse)
        assert response.status_code == 402  # noqa: PLR2004
        assert get_current_organization() is None

    def test_an_outer_binding_is_restored_rather_than_cleared(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        system_user, token = _auth_service().create_system_user(
            integration_name="scoping-teardown-nested",
            organization=organization,
            bypass_limits=True,
        )
        seen: list[Any] = []

        with organization_context(other_organization):
            _middleware(seen)(_graphql_request(f"Bearer {system_user.id}:{token}"))
            still_bound = get_current_organization()

        assert seen == [organization.pk]
        assert still_bound is not None
        assert still_bound.pk == other_organization.pk


@pytest.mark.django_db
class TestAnOrgLessCredentialMustBeAskedFor:
    """``organization=None`` is a request, never an oversight.

    ``SystemUser`` is the one scoped model that may hold ``organization=None``,
    and that row is a token with access to *every* organization. ``save()``
    therefore skips the mixin's stamp-or-raise -- but only for a caller who wrote
    the ``None``. Keying the exemption on ``organization_id is None`` instead
    would have made ``create(integration_name=..., ...)`` with the argument
    forgotten, inside a request serving one organization, mint a credential for
    all of them.
    """

    def test_a_forgotten_organization_raises_instead_of_going_global(
        self, organization: Organization
    ) -> None:
        with (
            organization_context(organization),
            pytest.raises(OrganizationNotFoundError, match="was not given"),
        ):
            SystemUser(
                integration_name="forgotten-arg",
                long_lived_token_hash="forgotten-hash",
            ).save()

        assert not SystemUser.original_manager.filter(integration_name="forgotten-arg").exists()

    def test_the_manager_refuses_it_too(self, organization: Organization) -> None:
        """The same row, written the way the services write one."""
        with (
            organization_context(organization),
            pytest.raises(OrganizationNotFoundError, match="was not given"),
        ):
            SystemUser.objects.create(
                integration_name="forgotten-arg-manager",
                long_lived_token_hash="forgotten-hash",
            )

        assert not SystemUser.original_manager.filter(
            integration_name="forgotten-arg-manager"
        ).exists()

    def test_writing_the_none_out_still_mints_the_global_credential(
        self, organization: Organization
    ) -> None:
        """The sanctioned path, exercised from inside a *bound* context.

        Binding an organization must not narrow a credential that asked for
        none -- that is the half of the old behaviour worth keeping.
        """
        with organization_context(organization):
            system_user, _token = _auth_service().create_system_user(
                integration_name="deliberately-org-less",
                organization=None,
                bypass_limits=True,
            )

        system_user.refresh_from_db()
        assert system_user.organization_id is None

    def test_a_persisted_org_less_row_can_still_be_updated(
        self, organization: Organization
    ) -> None:
        """``revoke``'s ``save(update_fields=[...])`` on a global token.

        The row's organization is a decision already recorded in the database, so
        re-saving it is not a new org-less write and does not need the marker.
        """
        system_user, _token = _auth_service().create_system_user(
            integration_name="org-less-revoke", organization=None, bypass_limits=True
        )
        reloaded = SystemUser.original_manager.get(pk=system_user.pk)

        reloaded.is_active = False
        reloaded.save(update_fields=["is_active"])

        assert SystemUser.original_manager.get(pk=system_user.pk).is_active is False

    def test_naming_an_organization_is_unaffected(self, organization: Organization) -> None:
        system_user = SystemUser.objects.create(
            organization=organization,
            integration_name="named-organization",
            long_lived_token_hash="named-hash",
        )

        assert system_user.organization_id == organization.pk


@pytest.mark.django_db
class TestAnUnboundScopedReadRaisesRatherThanLeaking:
    """Strict mode's whole point, stated on this app's own model.

    Without ``STRICT_ORGANIZATION_FILTER`` the read below would return an empty
    queryset, which is indistinguishable from "this organization owns no tokens"
    -- and the caller would carry on. The scoped read must also *not* answer with
    another organization's rows, which the second half pins.
    """

    def test_unbound_reads_raise(self, organization: Organization) -> None:
        baker.make(
            SystemUser,
            organization=organization,
            integration_name="strict-mode-probe",
            long_lived_token_hash="strict-hash",
        )

        with pytest.raises(OrganizationNotFoundError):
            SystemUser.objects.count()

    def test_a_bound_read_sees_only_its_own_organizations_rows(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        mine = baker.make(
            SystemUser,
            organization=organization,
            integration_name="strict-mine",
            long_lived_token_hash="strict-mine-hash",
        )
        theirs = baker.make(
            SystemUser,
            organization=other_organization,
            integration_name="strict-theirs",
            long_lived_token_hash="strict-theirs-hash",
        )

        with organization_context(organization):
            visible = set(SystemUser.objects.values_list("pk", flat=True))

        assert mine.pk in visible
        assert theirs.pk not in visible

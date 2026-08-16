"""``common.org_retrievers.retrieve_by_x_organization_id``.

Registered in ``SHARED_SCHEMA_ORGANIZATIONS['ORGANIZATION_RETRIEVERS']``, which
is what the package's resolver -- reached from
``TenantScopedViewMixin.perform_authentication`` -- calls to turn a request into
an organization. Pinned here because the contract it has to hold is easy to get
subtly wrong.

The contract that matters most is the one it does **not** share with the
package's own ``retrieve_by_http_header``: an unknown organization id returns
``None`` here, where that one raises ``OrganizationNotFoundError``. That is
deliberate. ``TenantScopedViewMixin`` owns the 403 for "the header names an
organization you are not a member of", and it can only make that call because it
sees the caller's memberships -- which a retriever does not.
"""

from django.conf import settings
from django.test import RequestFactory

import pytest
from model_bakery import baker

from common.constants import ACTIVE_ORG_HEADER, ACTIVE_ORG_HEADER_META_KEY
from common.org_retrievers import retrieve_by_x_organization_id
from organizations.models import Organization


@pytest.fixture
def request_factory() -> RequestFactory:
    return RequestFactory()


def _request(request_factory: RequestFactory, header_value=None):
    extra = {} if header_value is None else {ACTIVE_ORG_HEADER_META_KEY: header_value}
    return request_factory.get("/", **extra)


class TestTheHeaderConstant:
    def test_the_wire_name_is_x_organization_id(self):
        assert ACTIVE_ORG_HEADER == "X-Organization-Id"

    def test_the_meta_key_matches_djangos_spelling(self):
        assert ACTIVE_ORG_HEADER_META_KEY == "HTTP_X_ORGANIZATION_ID"

    def test_the_view_mixin_reads_the_same_constant(self):
        """One definition, shared by request resolution, the OpenAPI parameter
        and this retriever, so the documented header cannot drift from the one
        the code looks for."""
        from common.utils import view_utils

        assert view_utils.ACTIVE_ORG_HEADER is ACTIVE_ORG_HEADER


class TestItIsRegistered:
    def test_it_is_the_only_configured_retriever(self):
        """Every retriever the package ships answers a question we do not ask:
        ``retrieve_by_domain`` is subdomain tenancy (which this project does not do), ``retrieve_by_http_header`` reads a slug from a
        different header, and ``retrieve_by_session`` reads the session."""
        assert settings.SHARED_SCHEMA_ORGANIZATIONS["ORGANIZATION_RETRIEVERS"] == [
            "common.org_retrievers.retrieve_by_x_organization_id"
        ]


@pytest.mark.django_db
class TestRetrieveByXOrganizationId:
    def test_resolves_a_valid_header(self, request_factory):
        organization = baker.make(Organization)

        resolved = retrieve_by_x_organization_id(_request(request_factory, str(organization.id)))

        assert resolved == organization

    def test_returns_none_when_the_header_is_absent(self, request_factory):
        assert retrieve_by_x_organization_id(_request(request_factory)) is None

    def test_returns_none_when_the_header_is_empty(self, request_factory):
        assert retrieve_by_x_organization_id(_request(request_factory, "")) is None

    def test_returns_none_when_the_header_is_not_an_integer(self, request_factory):
        for value in ("not-a-number", "12.5", "1,2", " ", "1 OR 1=1"):
            assert retrieve_by_x_organization_id(_request(request_factory, value)) is None

    def test_returns_none_and_does_not_raise_on_an_unknown_id(self, request_factory):
        """Deliberately unlike the package's ``retrieve_by_http_header``, which
        raises. See the module docstring: raising here would turn the mixin's
        403 into a 500 on the paths that opt out of header enforcement."""
        organization = baker.make(Organization)
        unknown_id = organization.id + 10_000

        assert retrieve_by_x_organization_id(_request(request_factory, str(unknown_id))) is None

    def test_resolves_the_named_organization_and_not_merely_the_first_one(self, request_factory):
        baker.make(Organization)
        wanted = baker.make(Organization)

        assert retrieve_by_x_organization_id(_request(request_factory, str(wanted.id))) == wanted

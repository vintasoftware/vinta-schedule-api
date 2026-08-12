"""Tests for ``common.org_retrievers.retrieve_by_x_organization_id``.

Phase 1b of the vinta-django-orgs migration ships this retriever tested but
unregistered — see the module docstring for why. These tests only exercise
the retriever function directly (no view, no ``ORGANIZATION_RETRIEVERS``
wiring exists yet).
"""

from __future__ import annotations

from django.http import HttpRequest
from django.test import RequestFactory

import pytest

from common.org_retrievers import ORGANIZATION_ID_HEADER, retrieve_by_x_organization_id
from tenancy.models import Organization


pytestmark = pytest.mark.django_db

factory = RequestFactory()


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Acme")


def _request_with_header(value: str | None) -> HttpRequest:
    if value is None:
        return factory.get("/")
    return factory.get("/", headers={ORGANIZATION_ID_HEADER: value})


class TestRetrieveByXOrganizationId:
    def test_resolves_a_valid_header(self, organization: Organization):
        request = _request_with_header(str(organization.pk))

        resolved = retrieve_by_x_organization_id(request)

        assert resolved == organization

    def test_missing_header_returns_none(self, organization: Organization):
        request = _request_with_header(None)

        assert retrieve_by_x_organization_id(request) is None

    def test_empty_header_returns_none(self, organization: Organization):
        request = _request_with_header("")

        assert retrieve_by_x_organization_id(request) is None

    def test_non_integer_header_returns_none(self, organization: Organization):
        request = _request_with_header("not-an-int")

        assert retrieve_by_x_organization_id(request) is None

    def test_unknown_id_returns_none_without_raising(self, organization: Organization):
        unknown_id = organization.pk + 999_999
        request = _request_with_header(str(unknown_id))

        assert retrieve_by_x_organization_id(request) is None

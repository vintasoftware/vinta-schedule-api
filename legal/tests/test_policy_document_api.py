"""Integration tests for the read-only PolicyDocument REST API (Phase 2).

Covers:
- ``latest`` (list): one row per document_type, at its highest version. Public.
- ``latest_by_type`` (retrieve-by-type): highest version of a single type. Public.
  404 on an unknown / empty type.
- ``retrieve`` (by id): exact version by primary key. Authenticated.
- ``list`` (history): full version history, newest-first per type, optional
  ``?document_type=`` filter. Authenticated.
- Auth split: public ``latest``/``latest_by_type`` reachable unauthenticated;
  ``list``/``retrieve`` refuse unauthenticated callers with 401.
"""

from django.urls import reverse

import pytest
from rest_framework import status

from legal.factories import PolicyDocumentFactory
from legal.models import PolicyDocumentType


pytestmark = pytest.mark.django_db

LATEST_LIST_URL = "api:PolicyDocuments-latest"
LATEST_BY_TYPE_URL = "api:PolicyDocuments-latest-by-type"
LIST_URL = "api:PolicyDocuments-list"
DETAIL_URL = "api:PolicyDocuments-detail"


def _latest_list_url() -> str:
    return reverse(LATEST_LIST_URL)


def _latest_by_type_url(document_type: str) -> str:
    return reverse(LATEST_BY_TYPE_URL, kwargs={"document_type": document_type})


def _list_url() -> str:
    return reverse(LIST_URL)


def _detail_url(pk: int) -> str:
    return reverse(DETAIL_URL, args=[pk])


class TestLatestList:
    """GET /policy-documents/latest/ — one row per type, at its highest version."""

    def test_returns_one_row_per_type_at_highest_version(self, anonymous_client):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        latest_privacy = factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=2)
        latest_terms = factory.create(document_type=PolicyDocumentType.TERMS_OF_USE, version=1)
        factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        latest_sms = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)

        response = anonymous_client.get(_latest_list_url())

        assert response.status_code == status.HTTP_200_OK
        results = response.json()
        assert isinstance(results, list)
        ids = {row["id"] for row in results}
        assert ids == {latest_privacy.id, latest_terms.id, latest_sms.id}

    def test_reachable_unauthenticated(self, anonymous_client):
        response = anonymous_client.get(_latest_list_url())
        assert response.status_code == status.HTTP_200_OK


class TestLatestByType:
    """GET /policy-documents/latest/{document_type}/ — highest version of one type."""

    def test_returns_highest_version(self, anonymous_client):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        latest_sms = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)

        response = anonymous_client.get(_latest_by_type_url(PolicyDocumentType.SMS_CONSENT))

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == latest_sms.id
        assert data["version"] == 2
        assert data["document_type"] == PolicyDocumentType.SMS_CONSENT

    def test_unknown_type_returns_404(self, anonymous_client):
        response = anonymous_client.get(_latest_by_type_url("not_a_real_type"))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_empty_type_returns_404(self, anonymous_client):
        """A known enum value with zero published rows also 404s."""
        response = anonymous_client.get(_latest_by_type_url(PolicyDocumentType.TERMS_OF_USE))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_reachable_unauthenticated(self, anonymous_client):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        response = anonymous_client.get(_latest_by_type_url(PolicyDocumentType.SMS_CONSENT))
        assert response.status_code == status.HTTP_200_OK


class TestRetrieveById:
    """GET /policy-documents/{id}/ — exact version by primary key. Authenticated."""

    def test_returns_exact_version(self, auth_client):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        v2 = factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=2)

        response = auth_client.get(_detail_url(v2.id))

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == v2.id
        assert data["version"] == 2
        assert data["title"] == v2.title
        assert data["body_markdown"] == v2.body_markdown

    def test_unauthenticated_returns_401(self, anonymous_client):
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        response = anonymous_client.get(_detail_url(document.id))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestHistoryList:
    """GET /policy-documents/ — full version history, newest first; optional filter."""

    def test_returns_all_versions_newest_first(self, auth_client):
        factory = PolicyDocumentFactory()
        v1 = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        v2 = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)
        v3 = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=3)

        response = auth_client.get(_list_url())

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        results = data.get("results", data)
        versions = [row["version"] for row in results]
        assert versions == [3, 2, 1]
        assert {row["id"] for row in results} == {v1.id, v2.id, v3.id}

    def test_document_type_filter(self, auth_client):
        factory = PolicyDocumentFactory()
        sms = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)

        response = auth_client.get(_list_url(), {"document_type": PolicyDocumentType.SMS_CONSENT})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        results = data.get("results", data)
        assert len(results) == 1
        assert results[0]["id"] == sms.id

    def test_unauthenticated_returns_401(self, anonymous_client):
        response = anonymous_client.get(_list_url())
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_invalid_document_type_filter_returns_400(self, auth_client):
        response = auth_client.get(_list_url(), {"document_type": "not_a_real_type"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

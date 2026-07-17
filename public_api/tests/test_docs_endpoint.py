from pathlib import Path

from django.conf import settings

import pytest
from rest_framework import status

from public_api.docs_content import _ALLOWLIST
from webhooks.constants import WebhookEventType


CONCEPTS_DIR = Path(settings.BASE_DIR) / "docs" / "concepts"

EXPECTED_SLUGS = (
    "availability",
    "calendar-bundles",
    "calendar-groups",
    "calendars",
    "events",
    "recurrence",
)


@pytest.mark.django_db
class TestPublicApiDocsList:
    def test_list_returns_all_six_docs(self, anonymous_client):
        response = anonymous_client.get("/public-api-docs/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [entry["slug"] for entry in body] == sorted(EXPECTED_SLUGS)
        for entry in body:
            assert set(entry.keys()) == {"slug", "title"}
            assert entry["title"]

    def test_list_succeeds_unauthenticated(self, anonymous_client):
        """Explicitly assert no credentials are required.

        Every other route in this app requires auth; a future global
        default-permission change must fail here first.
        """
        assert anonymous_client.get("/public-api-docs/").status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestPublicApiDocsRetrieve:
    def test_retrieve_returns_markdown_verbatim(self, anonymous_client):
        response = anonymous_client.get("/public-api-docs/calendar-groups/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["slug"] == "calendar-groups"
        assert body["title"]
        assert body["markdown"] == (CONCEPTS_DIR / "calendar-groups.md").read_text()

    def test_retrieve_succeeds_unauthenticated(self, anonymous_client):
        response = anonymous_client.get("/public-api-docs/calendar-groups/")

        assert response.status_code == status.HTTP_200_OK

    def test_retrieve_unknown_slug_returns_404(self, anonymous_client):
        response = anonymous_client.get("/public-api-docs/does-not-exist/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.parametrize(
        "path",
        [
            "/public-api-docs/../settings/",
            "/public-api-docs/..%2Fsettings/",
            "/public-api-docs/..%2F..%2Fsettings/",
            "/public-api-docs/%2Fetc%2Fpasswd/",
            "/public-api-docs//etc/passwd/",
            "/public-api-docs/../../README/",
            "/public-api-docs/../../AGENTS/",
            "/public-api-docs/../../CODE_OF_CONDUCT/",
        ],
    )
    def test_traversal_payloads_never_read_a_file_outside_allowlist(self, anonymous_client, path):
        response = anonymous_client.get(path)

        assert response.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestPublicApiDocsWebhookEvents:
    def test_returns_one_entry_per_member_in_declaration_order(self, anonymous_client):
        response = anonymous_client.get("/public-api-docs/webhook-events/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        expected_values = [member.value for member in WebhookEventType]
        assert [entry["value"] for entry in body] == expected_values
        for entry in body:
            assert set(entry.keys()) == {"value", "label", "description"}
            assert entry["label"]
            assert entry["description"]

    def test_succeeds_unauthenticated(self, anonymous_client):
        assert (
            anonymous_client.get("/public-api-docs/webhook-events/").status_code
            == status.HTTP_200_OK
        )

    def test_webhook_events_slug_does_not_collide_with_a_concept_doc(self):
        """Checks the reserved-slug trap in API Design 4.2.

        ``webhook-events`` is registered as a detail=False action ahead of the
        ``{slug}`` detail route. If a concept doc named ``webhook-events.md`` were ever
        added, it would be shadowed and unreachable — this test fails loudly instead.
        """
        assert "webhook-events" not in _ALLOWLIST

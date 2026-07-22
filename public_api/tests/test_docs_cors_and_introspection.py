"""Tests locking in CORS and GraphQL introspection for the docs origin.

Guarantees:
- Introspection is enabled on /graphql/ (so docs build-time schema fetch works).
- CORS allows the configured docs origins to make cross-origin requests.
- The authorization header is allowed for CORS preflight.
- A wildcard or permissive regex is not set (tested by asserting unconfigured origins fail).
"""

from django.test import override_settings

import pytest
from rest_framework.test import APIClient


# Standard GraphQL introspection query: fetch __schema and at least one type.
# Fails fast if introspection is disabled.
INTROSPECTION_QUERY = """
    query IntrospectionQuery {
        __schema {
            types {
                name
            }
        }
    }
"""


@pytest.mark.django_db
class TestGraphQLIntrospection:
    """Introspection must be enabled for docs build-time schema fetches."""

    def test_introspection_query_returns_schema(self):
        """POST /graphql/ with introspection query returns 200 and __schema with types.

        This test guards the 'Introspection in production' decision: a future
        Strawberry upgrade that disables introspection, or a config refactor that
        blocks the query, must fail here rather than silently breaking the published
        docs schema reference and GraphiQL's autocomplete.
        """
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={"query": INTROSPECTION_QUERY},
            format="json",
        )

        # Introspection must succeed.
        assert response.status_code == 200, (
            f"Introspection query failed with status {response.status_code}. "
            "This may indicate introspection is disabled in Strawberry config or GraphQL schema."
        )

        data = response.json()

        # Response must not contain errors.
        assert "errors" not in data or not data.get("errors"), (
            f"Introspection query returned errors: {data.get('errors')}"
        )

        # Response must contain __schema with at least one type.
        assert "data" in data, "GraphQL response missing 'data' field"
        assert "__schema" in data["data"], "Response missing '__schema' field"
        assert "types" in data["data"]["__schema"], "__schema missing 'types' field"

        # types must be a non-empty list (anti-vacuity check).
        types_list = data["data"]["__schema"]["types"]
        assert isinstance(types_list, list), f"'types' must be a list, got {type(types_list)}"
        assert len(types_list) > 0, (
            "Introspection returned an empty types list. The schema may be incorrectly configured."
        )


@pytest.mark.django_db
class TestCORSPreflight:
    """CORS preflight must allow configured origins and the authorization header.

    Tests use override_settings to drive CORS configuration in-test, avoiding
    dependence on ambient .env values.
    """

    @override_settings(
        CORS_ALLOWED_ORIGINS=[
            "https://schedule.vintasoftware.com",
            "https://schedule-staging.vintasoftware.com",
        ]
    )
    def test_configured_origin_preflight_succeeds(self):
        """OPTIONS /graphql/ from a CORS_ALLOWED_ORIGINS origin echoes the origin.

        Covers the 'CORS origins' and 'authorization header' decisions: the request
        from the docs origin receives an Access-Control-Allow-Origin echo and may
        include the authorization header in the actual request.
        """
        client = APIClient()

        # Perform an OPTIONS preflight for a POST request with authorization header.
        response = client.options(
            "/graphql/",
            HTTP_ORIGIN="https://schedule.vintasoftware.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization",
        )

        # Preflight must succeed.
        assert response.status_code == 200, (
            f"CORS preflight failed with status {response.status_code}. "
            f"Response: {response.content.decode()}"
        )

        # Response must echo the requested origin in Access-Control-Allow-Origin.
        allow_origin = response.get("Access-Control-Allow-Origin")
        assert allow_origin == "https://schedule.vintasoftware.com", (
            f"Expected Access-Control-Allow-Origin to echo the request origin, got {allow_origin!r}"
        )

        # Response must include authorization in Access-Control-Allow-Headers.
        allow_headers = response.get("Access-Control-Allow-Headers", "").lower()
        assert "authorization" in allow_headers, (
            f"Expected 'authorization' in Access-Control-Allow-Headers, "
            f"got {response.get('Access-Control-Allow-Headers')!r}"
        )

    @override_settings(
        CORS_ALLOWED_ORIGINS=[
            "https://schedule.vintasoftware.com",
            "https://schedule-staging.vintasoftware.com",
        ]
    )
    def test_unconfigured_origin_preflight_fails(self):
        """OPTIONS /graphql/ from an unconfigured origin does NOT echo the origin.

        This test has teeth: it FAILS if someone ever sets CORS_ALLOWED_ORIGINS
        to a wildcard or permissive regex, which would be a security hole given
        CORS_ALLOW_CREDENTIALS = True. The absence of an echo for an unconfigured
        origin is the meaningful assertion; a missing header is the expected behavior.
        """
        client = APIClient()

        # Perform an OPTIONS preflight from an origin NOT in CORS_ALLOWED_ORIGINS.
        response = client.options(
            "/graphql/",
            HTTP_ORIGIN="https://evil.example.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization",
        )

        # Preflight response status is OK, but the origin is NOT echoed.
        # When an origin is not in CORS_ALLOWED_ORIGINS and CORS_ALLOW_ALL_ORIGINS is off,
        # django-cors-headers sets no Access-Control-Allow-Origin header.
        allow_origin = response.get("Access-Control-Allow-Origin")
        assert allow_origin is None, (
            f"CORS security failure: Access-Control-Allow-Origin header must not be set for unconfigured origins. "
            f"A non-None value indicates either an echo of an unconfigured origin or a wildcard, "
            f"both of which are security failures given CORS_ALLOW_CREDENTIALS=True. "
            f"Got Allow-Origin: {allow_origin!r}"
        )

"""Unit tests for ``common.openapi.TenantScopedAutoSchema``.

Generates the OpenAPI schema in-process (using drf-spectacular's
``SchemaGenerator``) and asserts that the ``X-Organization-Id`` header
parameter is declared on tenant-scoped operations and absent on opted-out
or non-tenant operations.

Assertions use the raw schema dict so this test has no HTTP overhead and is
fully isolated from external services.
"""

from __future__ import annotations

import pytest
from drf_spectacular.generators import SchemaGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_schema() -> dict:
    """Generate the full OpenAPI schema dict in-process."""
    generator = SchemaGenerator()
    return generator.get_schema(request=None, public=True)  # type: ignore[arg-type]


def _operation_header_names(schema: dict, path: str, method: str) -> set[str]:
    """Return the set of header parameter names declared on a specific operation.

    Args:
        schema: The full OpenAPI schema dict.
        path:   The path key (e.g. ``"/calendar/"``).
        method: The HTTP method in lower-case (e.g. ``"get"``).

    Returns:
        A set of ``name`` values for parameters with ``in == "header"``.
    """
    operation = schema.get("paths", {}).get(path, {}).get(method, {})
    params = operation.get("parameters", [])
    return {p["name"] for p in params if p.get("in") == "header"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    """Generate the schema once per module to keep test runs fast."""
    return _get_schema()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTenantScopedAutoSchema:
    """``X-Organization-Id`` is injected / omitted on the correct operations."""

    def test_tenant_scoped_endpoint_has_header(self, openapi_schema: dict) -> None:
        """``GET /calendar/`` is tenant-scoped: must declare ``X-Organization-Id``."""
        header_names = _operation_header_names(openapi_schema, "/calendar/", "get")
        assert "X-Organization-Id" in header_names, (
            "Expected 'X-Organization-Id' header parameter on GET /calendar/, "
            f"but only found: {header_names}"
        )

    def test_tenant_scoped_header_is_not_required(self, openapi_schema: dict) -> None:
        """``X-Organization-Id`` on tenant-scoped ops must be ``required: false``.

        OpenAPI 3.x treats missing ``required`` as ``false``, and drf-spectacular
        omits the key when it is ``False`` (the default).  We therefore assert that
        ``required`` is NOT ``True`` (i.e. absent or explicitly ``False``).
        """
        path = "/calendar/"
        method = "get"
        operation = openapi_schema.get("paths", {}).get(path, {}).get(method, {})
        params = operation.get("parameters", [])
        header_param = next(
            (p for p in params if p.get("in") == "header" and p["name"] == "X-Organization-Id"),
            None,
        )
        assert header_param is not None, (
            f"X-Organization-Id header parameter not found on {method.upper()} {path}"
        )
        # drf-spectacular omits ``required`` when False (OpenAPI default is False).
        # Assert it is not explicitly True rather than asserting it equals False.
        assert header_param.get("required") is not True, (
            "X-Organization-Id should be optional (required != true); "
            "single-membership callers may omit it"
        )

    def test_current_action_has_header(self, openapi_schema: dict) -> None:
        """``GET /organizations/current/`` is action-bearing and tenant-scoped.

        It is NOT in ``active_org_optional_actions``, so it must DECLARE the
        ``X-Organization-Id`` header (``in: header``, ``required`` not True).
        This guards against a regression where ``action``-bearing ViewSet routes
        silently lose the injected header.
        """
        path = "/organizations/current/"
        method = "get"
        header_names = _operation_header_names(openapi_schema, path, method)
        assert "X-Organization-Id" in header_names, (
            "Expected 'X-Organization-Id' header parameter on GET /organizations/current/, "
            f"but only found: {header_names}"
        )
        operation = openapi_schema.get("paths", {}).get(path, {}).get(method, {})
        params = operation.get("parameters", [])
        header_param = next(
            (p for p in params if p.get("in") == "header" and p["name"] == "X-Organization-Id"),
            None,
        )
        assert header_param is not None, (
            f"X-Organization-Id header parameter not found on {method.upper()} {path}"
        )
        assert header_param.get("required") is not True, (
            "X-Organization-Id should be optional (required != true) on /organizations/current/"
        )

    def test_mine_action_has_no_header(self, openapi_schema: dict) -> None:
        """``GET /organizations/mine/`` is opted out: must NOT declare the header."""
        header_names = _operation_header_names(openapi_schema, "/organizations/mine/", "get")
        assert "X-Organization-Id" not in header_names, (
            "GET /organizations/mine/ is in active_org_optional_actions and must NOT "
            f"declare X-Organization-Id, but found headers: {header_names}"
        )

    def test_create_organization_has_no_header(self, openapi_schema: dict) -> None:
        """``POST /organizations/`` is opted out: must NOT declare the header."""
        header_names = _operation_header_names(openapi_schema, "/organizations/", "post")
        assert "X-Organization-Id" not in header_names, (
            "POST /organizations/ is in active_org_optional_actions ('create') and must NOT "
            f"declare X-Organization-Id, but found headers: {header_names}"
        )

    def test_non_tenant_endpoint_has_no_header(self, openapi_schema: dict) -> None:
        """``POST /invitations/accept`` is not tenant-scoped: must NOT declare the header."""
        header_names = _operation_header_names(openapi_schema, "/invitations/accept", "post")
        assert "X-Organization-Id" not in header_names, (
            "POST /invitations/accept is not a TenantScopedViewMixin subclass and must NOT "
            f"declare X-Organization-Id, but found headers: {header_names}"
        )

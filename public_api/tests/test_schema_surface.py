"""Test to ensure can_invite_organizations is never exposed via any API surface."""

import pytest
from graphql import GraphQLInputObjectType


def collect_all_graphql_field_names() -> set[str]:
    """Introspect the fully-built Strawberry schema's graphql-core type map.

    Enumerates every input and output type and all their fields to ensure
    comprehensive field discovery across all GraphQL types.
    """
    from public_api.schema import schema

    names: set[str] = set()
    for gql_type in schema._schema.type_map.values():
        fields = getattr(gql_type, "fields", None)
        if not fields:
            continue
        for field_name in fields:  # dict keyed by field name
            names.add(field_name.lower())
    return names


def collect_output_graphql_field_names() -> set[str]:
    """Introspect only OUTPUT (non-input) GraphQL field names.

    Input object types are arguments clients SEND (e.g. UpdateBrandingInput),
    not data the API serializes back. The §4.6 "never exposed" invariant is
    about response data, so allowlist guards scan output types only — otherwise
    a legitimate write-only input field would trip the guard.
    """
    from public_api.schema import schema

    names: set[str] = set()
    for gql_type in schema._schema.type_map.values():
        if isinstance(gql_type, GraphQLInputObjectType):
            continue
        fields = getattr(gql_type, "fields", None)
        if not fields:
            continue
        for field_name in fields:
            names.add(field_name.lower())
    return names


@pytest.mark.django_db
class TestCanInviteOrganizationsNotExposed:
    """Guard tests to ensure can_invite_organizations is not reachable via API."""

    def test_can_invite_organizations_not_in_graphql_types(self):
        """Verify that can_invite_organizations is absent from GraphQL types.

        This test introspects the public GraphQL schema to ensure the flag is not
        exposed as a queryable/mutable field in any Input or Output type.
        """
        # Collect all field names from the fully-built GraphQL schema
        field_names = collect_all_graphql_field_names()

        # Verify introspection actually found fields (anti-vacuity check)
        assert field_names, "schema introspection returned no fields — guard would be vacuous"

        # canInviteOrganizations should not appear in any form
        forbidden_variations = [
            "caninviteorganizations",
            "can_invite_organizations",
        ]
        for variation in forbidden_variations:
            assert variation not in field_names, (
                f"can_invite_organizations (as {variation}) must not be exposed in the GraphQL schema. "
                "Check that no mutation or query includes this field."
            )

    def test_return_url_allowlist_not_in_graphql_types(self):
        """Verify return_url_allowlist is absent from every GraphQL type.

        Phase 2a of the Organization Auth-Area Branding plan dropped
        ``return_url_allowlist`` in favor of a single ``redirect_url`` destination —
        see ``test_redirect_url_replaces_return_url_allowlist`` below. Nothing should
        ever reintroduce the old field name, on an output type or otherwise.
        """
        field_names = collect_all_graphql_field_names()
        assert field_names, "schema introspection returned no fields — guard would be vacuous"

        forbidden_variations = [
            "returnurlallowlist",
            "return_url_allowlist",
        ]
        for variation in forbidden_variations:
            assert variation not in field_names, (
                f"return_url_allowlist (as {variation}) must not be exposed in the GraphQL schema. "
                "It was replaced by redirect_url in Phase 2a."
            )

    def test_validate_return_url_query_not_in_schema(self):
        """Verify validateReturnUrl is absent from the schema entirely (Phase 2a).

        It answered a yes/no question against ``return_url_allowlist``, which no
        longer exists — there is no caller-supplied redirect target left to
        validate. Checked against every type's field list (not just output types)
        since it was itself a root Query field.
        """
        field_names = collect_all_graphql_field_names()
        assert field_names, "schema introspection returned no fields — guard would be vacuous"

        forbidden_variations = [
            "validatereturnurl",
            "validate_return_url",
        ]
        for variation in forbidden_variations:
            assert variation not in field_names, (
                f"validateReturnUrl (as {variation}) must not be exposed in the GraphQL schema. "
                "It was removed in Phase 2a along with return_url_allowlist."
            )

        # The result type it used to return must be gone too — not just unreferenced.
        from public_api.schema import schema

        type_names = {t for t in schema._schema.type_map}
        assert "ValidateReturnUrlResult" not in type_names

    def test_redirect_url_replaces_return_url_allowlist(self):
        """redirect_url is reachable on UpdateBrandingInput (the write-only surface
        that replaced return_url_allowlist), naming the field-swap contract."""
        from public_api.schema import schema

        update_branding_input = schema._schema.type_map.get("UpdateBrandingInput")
        assert update_branding_input is not None, (
            "UpdateBrandingInput must still be part of the schema"
        )
        fields = getattr(update_branding_input, "fields", None)
        assert fields, "UpdateBrandingInput introspection returned no fields"
        assert "redirectUrl" in fields

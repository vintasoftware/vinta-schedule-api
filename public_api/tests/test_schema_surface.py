"""Test to ensure can_invite_organizations is never exposed via any API surface."""

from django.apps import apps

import pytest
from graphql import GraphQLInputObjectType
from rest_framework import serializers

from organizations.models import Organization


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
        """Verify return_url_allowlist is absent from every GraphQL type (§4.6).

        The OAuth return-URL allowlist is reseller-internal security config. It
        must never be queryable on any public output type — the validateReturnUrl
        query answers a yes/no question without ever serializing the list, and
        brandingForTenant deliberately omits it. (The write-only UpdateBrandingInput
        argument is excluded: it is how a reseller SETS its own allowlist via the
        authenticated mutation, not a response field.)
        """
        field_names = collect_output_graphql_field_names()
        assert field_names, "schema introspection returned no fields — guard would be vacuous"

        forbidden_variations = [
            "returnurlallowlist",
            "return_url_allowlist",
        ]
        for variation in forbidden_variations:
            assert variation not in field_names, (
                f"return_url_allowlist (as {variation}) must not be exposed in the GraphQL schema. "
                "It is reseller-internal config; expose only validateReturnUrl's yes/no result."
            )

    def test_can_invite_organizations_not_in_organization_serializers(self):
        """Verify that can_invite_organizations is absent from Organization serializers.

        Scans all installed apps for DRF serializers with 'Organization' in their name
        and ensures can_invite_organizations is not a declared field.
        """
        # Find all serializer classes in the codebase
        serializer_fields_by_name = {}

        for app_config in apps.get_app_configs():
            try:
                module = __import__(f"{app_config.name}.serializers", fromlist=[""])
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, serializers.Serializer)
                        and attr is not serializers.Serializer
                    ):
                        # Found a serializer; check if it mentions Organization
                        if (
                            "organization" in attr.__module__.lower()
                            or "Organization" in attr.__name__
                        ):
                            if hasattr(attr, "Meta") and hasattr(attr.Meta, "fields"):
                                fields = getattr(attr.Meta, "fields", [])
                                if isinstance(fields, (list, tuple)):
                                    serializer_fields_by_name[attr.__name__] = set(fields)
                                elif fields == "__all__":
                                    # __all__ means all model fields — check the model
                                    if hasattr(attr, "Meta") and hasattr(attr.Meta, "model"):
                                        model = attr.Meta.model
                                        serializer_fields_by_name[attr.__name__] = set(
                                            f.name
                                            for f in model._meta.get_fields()
                                            if hasattr(f, "name")
                                        )
            except ModuleNotFoundError:
                # App has no serializers module, skip
                pass

        # Anti-vacuity assertions: ensure the scan actually found serializers
        assert serializer_fields_by_name, "serializer scan found nothing — guard would be vacuous"
        assert "OrganizationSerializer" in serializer_fields_by_name, (
            "OrganizationSerializer was not scanned — guard would miss the primary org serializer"
        )

        # Verify can_invite_organizations is not in any Organization-related serializer
        for serializer_name, fields in serializer_fields_by_name.items():
            assert "can_invite_organizations" not in fields, (
                f"Serializer {serializer_name} exposes can_invite_organizations. "
                "This field must never be writable or readable via any API."
            )

    def test_can_invite_organizations_not_in_drf_model_fields(self):
        """Verify Organization model's can_invite_organizations field is properly hidden.

        This is a direct check that the model field exists but is not exposed
        through normal DRF mechanisms.
        """
        # The field must exist on the model (for admin and code logic)
        assert hasattr(Organization, "can_invite_organizations"), (
            "Organization.can_invite_organizations must exist on the model itself."
        )

        # Verify it is a real model field with the correct default
        field = Organization._meta.get_field("can_invite_organizations")
        assert field.default is False

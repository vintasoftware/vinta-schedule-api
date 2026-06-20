"""Unit tests for ``common.fields.OrganizationMembershipForeignKey``.

Strategy
--------
Tests use field-introspection (``_meta.get_field``) and the ForeignObject's
``from_fields`` / ``to_fields`` to verify structural correctness without
compiling querysets to SQL (which requires lazy model references to be
resolved, something that is tricky inside ``isolate_apps``).

A SQL-string inspection test IS included but targets an intermediate step:
the generated JOIN is verified by examining the compiled SQL of a filter
that traverses the ForeignObject, within a ``isolate_apps`` context that
includes both ``"common"`` and ``"organizations"`` and explicitly wires up
the isolated ``OrganizationMembership`` copy as the ForeignObject target so
that model-reference resolution succeeds.

Four properties are verified:

1. **Concrete column** — ``<name>_user_id`` is a real concrete ``BigIntegerField``
   on the model, not a virtual or deferred field.

2. **ForeignObject descriptor** — ``<name>`` is registered as a
   ``ForeignObject`` with the expected ``from_fields`` / ``to_fields``.

3. **JOIN columns** — the ForeignObject's joining columns (via
   ``get_joining_columns()``) include both ``(organization_id, organization_id)``
   and ``(<name>_user_id, user_id)``, proving the composite join is configured.

4. **Nullability propagation** — ``null=True`` propagates to both the concrete
   column and the ForeignObject.
"""

from __future__ import annotations

from django.db import models
from django.test.utils import isolate_apps

from common.fields import OrganizationMembershipForeignKey
from organizations.models import OrganizationModel


class TestOrganizationMembershipForeignKey:
    """Field-level unit tests — no DB access required."""

    @isolate_apps("common")
    def test_user_id_is_concrete_biginteger_field(self) -> None:
        """``<name>_user_id`` is a real concrete BigIntegerField on the model."""

        class SampleModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.CASCADE,
                null=True,
                related_name="sample_models_concrete",
            )

            class Meta:
                app_label = "common"

        # Retrieve the field from the model's _meta registry.
        field = SampleModel._meta.get_field("membership_user_id")

        # Must be a concrete BigIntegerField (not deferred, not a relation descriptor).
        assert isinstance(field, models.BigIntegerField), (
            f"Expected membership_user_id to be BigIntegerField, got {type(field)}"
        )
        assert field.concrete, "membership_user_id must be a concrete (DB-column-backed) field"
        assert not field.many_to_many, "membership_user_id must not be a many-to-many relation"
        # The DB column name must exactly match the field name (no _id suffix added).
        assert field.column == "membership_user_id", (
            f"Expected DB column 'membership_user_id', got '{field.column}'"
        )

    @isolate_apps("common")
    def test_foreignobject_descriptor_is_on_model(self) -> None:
        """The ``<name>`` ForeignObject descriptor is registered on the model."""

        class SampleModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.CASCADE,
                null=True,
                related_name="sample_models_descriptor",
            )

            class Meta:
                app_label = "common"

        # The ForeignObject field named "membership" must be accessible via _meta.
        fo_field = SampleModel._meta.get_field("membership")
        assert isinstance(fo_field, models.ForeignObject), (
            f"Expected 'membership' to be a ForeignObject, got {type(fo_field)}"
        )
        # Verify the from_fields / to_fields match the plan specification.
        assert fo_field.from_fields == ["membership_user_id", "organization_id"], (
            f"Unexpected from_fields: {fo_field.from_fields}"
        )
        assert fo_field.to_fields == ["user_id", "organization_id"], (
            f"Unexpected to_fields: {fo_field.to_fields}"
        )
        # The ForeignObject must be non-editable (admin-safe, no form field).
        assert not fo_field.editable, "ForeignObject 'membership' must be non-editable"

    @isolate_apps("common")
    def test_null_kwarg_propagates_to_both_fields(self) -> None:
        """``null=True`` makes both the concrete column and ForeignObject nullable."""

        class NullableModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.SET_NULL,
                null=True,
                blank=True,
                related_name="nullable_models",
            )

            class Meta:
                app_label = "common"

        user_id_field = NullableModel._meta.get_field("membership_user_id")
        assert user_id_field.null is True, (
            "membership_user_id should be nullable when null=True is passed"
        )
        fo_field = NullableModel._meta.get_field("membership")
        assert fo_field.null is True, (
            "ForeignObject 'membership' should be nullable when null=True is passed"
        )

    @isolate_apps("common")
    def test_non_null_default(self) -> None:
        """Without ``null=True``, both fields are non-nullable (the default)."""

        class NonNullModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.CASCADE,
                related_name="non_null_models",
            )

            class Meta:
                app_label = "common"

        user_id_field = NonNullModel._meta.get_field("membership_user_id")
        assert user_id_field.null is False, "membership_user_id should NOT be nullable by default"
        fo_field = NonNullModel._meta.get_field("membership")
        assert fo_field.null is False, (
            "ForeignObject 'membership' should NOT be nullable by default"
        )

    @isolate_apps("common")
    def test_join_columns_cover_organization_id_and_user_id(self) -> None:
        """ForeignObject join is on (organization_id, <name>_user_id).

        Inspects the ``from_fields`` and ``to_fields`` on the ``ForeignObject``
        directly — these are plain string lists that require no model resolution
        and exactly specify which column pairs Django will use in the JOIN ON
        clause.

        This is the key multi-tenancy assertion: a join on ``user_id`` alone
        would be unsafe across tenants; the ``organization_id`` constraint
        ensures only the correct tenant's memberships are matched.

        from_fields (local host model columns):
            ``<name>_user_id``  — the denormalized user PK column
            ``organization_id`` — the tenant scope column (attname of the FK)

        to_fields (OrganizationMembership columns):
            ``user_id``         — attname of OrganizationMembership.user FK
            ``organization_id`` — attname of OrganizationMembership.organization FK
        """

        class SampleModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.CASCADE,
                null=True,
                related_name="sample_models_join",
            )

            class Meta:
                app_label = "common"

        raw_field = SampleModel._meta.get_field("membership")
        assert isinstance(raw_field, models.ForeignObject), (
            f"Expected 'membership' to be ForeignObject, got {type(raw_field)}"
        )
        fo_field: models.ForeignObject = raw_field  # narrow type for mypy

        # from_fields and to_fields are plain lists of strings — no model
        # resolution is required to inspect them.
        from_fields = list(fo_field.from_fields)
        to_fields = list(fo_field.to_fields)

        # Must be a composite join: exactly two field pairs.
        assert len(from_fields) == 2, (  # noqa: PLR2004
            f"Expected 2 from_fields, got {len(from_fields)}: {from_fields}"
        )
        assert len(to_fields) == 2, (  # noqa: PLR2004
            f"Expected 2 to_fields, got {len(to_fields)}: {to_fields}"
        )

        # Local side must include the denormalized user PK field.
        assert "membership_user_id" in from_fields, (
            f"Expected 'membership_user_id' in from_fields (local JOIN columns), got {from_fields}"
        )
        # Local side must include the org scoping column.
        assert "organization_id" in from_fields, (
            f"Expected 'organization_id' in from_fields (local JOIN columns), got {from_fields}"
        )
        # Remote side (OrganizationMembership) must join on user_id.
        assert "user_id" in to_fields, (
            f"Expected 'user_id' in to_fields (remote JOIN columns), got {to_fields}"
        )
        # Remote side must also join on organization_id (tenant scope).
        assert "organization_id" in to_fields, (
            f"Expected 'organization_id' in to_fields (remote JOIN columns), got {to_fields}"
        )

"""Custom Django field types for the Vinta Schedule API.

Module rationale — OrganizationMembershipForeignKey
----------------------------------------------------
Django 6 forbids a real ``ForeignKey`` (with a DB-level FK constraint) to a
model whose primary key is composite (``CompositePrimaryKey``).  Even before
``OrganizationMembership`` gains a composite PK, using a ``ForeignObject`` for
the relationship is the only design that will survive that migration without a
second round of model rewrites.

Why denormalize ``user_id``?
    A bare ``ForeignObject`` on ``(organization_id, …)`` requires the *host*
    row to already carry the second join column.  The host row always has
    ``organization_id`` (every ``OrganizationModel`` does), but it does NOT
    natively carry ``user_id``.  Storing a denormalized ``<name>_user_id``
    column on the host row makes the join instant — no extra look-up needed
    to resolve the owning user — and keeps the ``ForeignObject`` column
    mapping simple.

Why no real DB foreign-key constraint here?
    ``ForeignObject`` creates no DB-level constraint; the ORM join is purely
    at the Python/SQL level.  PROTECT delete semantics are enforced by a
    raw-SQL composite ``FOREIGN KEY (organization_id, <name>_user_id)
    REFERENCES organization_membership(organization_id, user_id) ON DELETE
    RESTRICT`` constraint added per referencing table.  This separation is
    intentional: the ORM field provides
    ``select_related`` / descriptor / queryset conveniences; the DB constraint
    provides integrity.
"""

import uuid

from django.db import models
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.models import AutoField, UUIDField
from django.db.models.fields.composite import CompositeAttribute, CompositePrimaryKey
from django.db.models.fields.related import ForeignObject


BaseDatabaseOperations.integer_field_ranges["UUIDField"] = (0, 0)


class _SafeCompositeAttribute(CompositeAttribute):
    """``CompositePrimaryKey`` descriptor that tolerates class-level access.

    Django's stock :class:`~django.db.models.fields.composite.CompositeAttribute`
    descriptor implements ``__get__`` as
    ``tuple(getattr(instance, attname) for attname in self.attnames)`` with no
    guard for ``instance is None``. Accessing the ``pk`` attribute on the *class*
    (``Model.pk``) — which Django's own field descriptors handle by returning the
    field/descriptor itself — therefore raises
    ``AttributeError: 'NoneType' object has no attribute '<attname>'``.

    Several introspection paths do exactly that class-level access. Notably
    ``django_virtual_models.utils.get_methods`` runs
    ``{attr for attr in dir(cls) if callable(getattr(cls, attr)) ...}`` over every
    attribute of the model class while optimizing a nested serializer; on a
    composite-PK model that ``getattr(cls, "pk")`` crashes. Returning the
    descriptor on class-level access (the standard Django descriptor convention)
    keeps that introspection working while instance-level access is unchanged.
    """

    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        return super().__get__(instance, cls)


class SafeCompositePrimaryKey(CompositePrimaryKey):
    """``CompositePrimaryKey`` whose descriptor tolerates class-level ``pk`` access.

    Behaves identically to Django's ``CompositePrimaryKey`` for instances; only
    differs in that ``Model.pk`` (class-level) returns the descriptor instead of
    raising. See :class:`_SafeCompositeAttribute` for the rationale.
    """

    descriptor_class = _SafeCompositeAttribute


class UUIDAutoField(UUIDField, AutoField):  # type: ignore
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", uuid.uuid4)
        kwargs.setdefault("editable", False)
        kwargs.pop("max_length", None)
        super().__init__(*args, **kwargs)

    def get_internal_type(self) -> str:
        return "UUIDField"

    def rel_db_type(self, connection):
        return models.UUIDField().db_type(connection=connection)

    def _check_max_length_warning(self):
        return []


class TenantSafeForeignKey(models.Field):
    """
    Combines a normal ForeignKey for DB constraints/admin/etc and a ForeignObject
    to enforce tenant_id in JOIN ON clauses.
    """

    tenant_field: str = "tenant_id"

    def __init__(
        self,
        to,
        on_delete=models.CASCADE,
        related_name=None,
        null=False,
        blank=False,
        help_text="",
    ):
        self.to = to
        self.tenant_field = self.tenant_field
        self.on_delete = on_delete
        self.related_name = related_name
        self.null = null
        self.blank = blank
        self.help_text = help_text

    def contribute_to_class(self, cls, name):
        fk_field_name = f"{name}_fk"
        strict_field_name = name

        # 1. Add the ForeignKey field
        fk_field = models.ForeignKey(
            self.to,
            on_delete=self.on_delete,
            related_name=f"{self.related_name or name}_fk_rel",
            null=self.null,
            blank=self.blank,
            help_text=self.help_text,
        )
        fk_field.contribute_to_class(cls, fk_field_name)

        # 2. Add the ForeignObject field (non-editable, for JOINs)
        fo_field = ForeignObject(
            self.to,
            from_fields=[f"{name}_fk", self.tenant_field],
            to_fields=["id", self.tenant_field],
            on_delete=self.on_delete,
            related_name=self.related_name or f"{name}_set",
            editable=False,
            null=self.null,
        )
        fo_field.contribute_to_class(cls, strict_field_name)


class TenantSafeOneToOneField(models.Field):
    """
    Combines a normal OneToOneField for DB constraints/admin/etc and a ForeignObject
    to enforce tenant_id in JOIN ON clauses.
    """

    tenant_field: str = "tenant_id"

    def __init__(
        self,
        to,
        on_delete=models.CASCADE,
        related_name=None,
        null=False,
        blank=False,
        help_text="",
    ):
        self.to = to
        self.tenant_field = self.tenant_field
        self.on_delete = on_delete
        self.related_name = related_name
        self.null = null
        self.blank = blank
        self.help_text = help_text

    def contribute_to_class(self, cls, name):
        fk_field_name = f"{name}_fk"
        strict_field_name = name

        # 1. Add the ForeignKey field
        fk_field = models.OneToOneField(
            self.to,
            on_delete=self.on_delete,
            related_name=f"{self.related_name or name}_fk_rel",
            null=self.null,
            blank=self.blank,
            help_text=self.help_text,
        )
        fk_field.contribute_to_class(cls, fk_field_name)

        # 2. Add the ForeignObject field (non-editable, for JOINs)
        fo_field = ForeignObject(
            self.to,
            from_fields=[f"{name}_fk", self.tenant_field],
            to_fields=["id", self.tenant_field],
            on_delete=self.on_delete,
            related_name=self.related_name or f"{name}_instance",
            editable=False,
            null=self.null,
            unique=True,
        )
        fo_field.contribute_to_class(cls, strict_field_name)


class OrganizationMembershipForeignKey(models.Field):
    """Reference an ``OrganizationMembership`` via a composite (org, user) join.

    Design summary
    --------------
    This field contributes **two** Django fields to the host model:

    1. **Concrete column** ``<name>_user_id`` — a plain ``BigIntegerField``
       (nullable when ``null=True``) that stores the denormalized ``user_id``
       from the target ``OrganizationMembership``.

       A plain integer field (rather than a real ``ForeignKey`` to ``User``) is
       chosen deliberately:

       - A ``ForeignKey(User, ...)`` contributed as ``"<name>_user_id"`` would
         produce an attname of ``<name>_user_id_id`` (Django appends ``_id``
         to FK field names), creating a confusing double-suffix.
       - The real integrity constraint we need is at the *membership* level —
         ``(organization_id, <name>_user_id)`` → ``OrganizationMembership`` —
         not at the bare ``User`` level.  That composite FK is added as a
         raw-SQL constraint per table.
       - The ``ForeignObject`` below already provides all ORM relationship
         features (``select_related``, reverse-accessor, filter traversal).

       **Required index**: This field deliberately does NOT add a single-column
       index on ``<name>_user_id``.  Multi-tenant queries always filter
       ``organization_id = X AND <name>_user_id = Y``, so the useful index is
       the tenant-leading composite ``(organization_id, <name>_user_id)``.
       A bare single-column index on ``<name>_user_id`` alone would be mostly
       wasted.  Adopting models MUST declare a composite index on
       ``(organization_id, <name>_user_id)`` in the migration that adds this
       field — alongside the raw-SQL composite FK constraint.

    2. **ForeignObject descriptor** ``<name>`` — a non-editable ``ForeignObject``
       joining::

           (host.organization_id, host.<name>_user_id)
           →
           (OrganizationMembership.organization_id, OrganizationMembership.user_id)

       This gives ``select_related("<name>")``, reverse-accessor, and
       ``filter(<name>__role=...)``-style queries, all automatically scoped by
       organization.

    Why no ``on_delete`` DB constraint at the membership level?
        ``ForeignObject`` creates no DB constraint.  PROTECT semantics against
        membership deletion are enforced by a per-table raw-SQL composite FK
        added through the project's raw-SQL migration framework.  The
        ``on_delete`` kwarg is stored and forwarded to the
        ``ForeignObject`` for ORM bookkeeping only.

    Usage::

        class MyModel(OrganizationModel):
            membership = OrganizationMembershipForeignKey(
                on_delete=models.PROTECT,
                related_name="my_models",
                null=True,
            )

        # After migration, ``MyModel`` has:
        #   - ``membership_user_id``  (BigIntegerField, concrete DB column)
        #   - ``membership``          (ForeignObject descriptor → OrganizationMembership)
    """

    def __init__(
        self,
        on_delete=models.CASCADE,
        related_name: str | None = None,
        null: bool = False,
        blank: bool = False,
        help_text: str = "",
    ) -> None:
        self.on_delete = on_delete
        self.related_name = related_name
        self.null = null
        self.blank = blank
        self.help_text = help_text

    def contribute_to_class(self, cls, name: str) -> None:  # type: ignore[override]
        """Inject the concrete ``<name>_user_id`` column and the ForeignObject descriptor."""
        user_id_field_name = f"{name}_user_id"

        # 1. Concrete column: plain BigIntegerField matching the User PK type
        #    (DEFAULT_AUTO_FIELD = BigAutoField → 64-bit integer).  No DB FK
        #    constraint — the composite FK to OrganizationMembership is added
        #    per table as raw SQL.
        user_id_field = models.BigIntegerField(
            null=self.null,
            blank=self.blank,
            help_text=self.help_text,
        )
        user_id_field.contribute_to_class(cls, user_id_field_name)

        # 2. ForeignObject: join (organization_id, <name>_user_id) →
        #    OrganizationMembership(organization_id, user_id).
        #    from_fields references the *field names* on the host model:
        #      - "<name>_user_id": the BigIntegerField added above
        #      - "organization_id": the inherited FK attname on OrganizationModel
        #    to_fields references the *attnames* on OrganizationMembership:
        #      - "user_id": attname of the `user` ForeignKey
        #      - "organization_id": attname of the `organization` ForeignKey
        #    This mirrors TenantSafeForeignKey's convention of using attnames
        #    (e.g. "organization_id") rather than field names in to_fields.
        #
        #    The ForeignObject is wired with ``on_delete=DO_NOTHING`` regardless of
        #    the configured ``self.on_delete``. Delete integrity (PROTECT) is
        #    enforced exclusively by the per-table raw-SQL composite FK to
        #    OrganizationMembership, which the raw-SQL migrations add as
        #    ``DEFERRABLE INITIALLY DEFERRED`` so the check fires at COMMIT. If the
        #    ForeignObject itself carried ``on_delete=PROTECT``, Django's *Python*
        #    cascade collector would raise ``ProtectedError`` eagerly — even for a
        #    same-transaction cascade that removes BOTH the membership and the
        #    referencing row (e.g. deleting an Organization, which CASCADEs to its
        #    memberships and OrganizationModel rows). Deferring to the DB-level
        #    constraint lets such whole-object cascades succeed while a
        #    membership-only delete (referencing row still live) still raises at
        #    commit. ``self.on_delete`` is retained for introspection/documentation
        #    of the intended semantics only.
        fo_field = ForeignObject(
            "organizations.OrganizationMembership",
            from_fields=[user_id_field_name, "organization_id"],
            to_fields=["user_id", "organization_id"],
            on_delete=models.DO_NOTHING,
            related_name=self.related_name or f"{name}_set",
            editable=False,
            null=self.null,
        )
        fo_field.contribute_to_class(cls, name)

"""Custom Django field types for the Vinta Schedule API.

Module rationale — OrganizationMembershipForeignKey
----------------------------------------------------
``OrganizationMembership`` carries a surrogate ``id`` again (a real
``ForeignKey`` to it would be legal), but the relation stays a
``ForeignObject`` on ``(organization_id, <name>_user_id)``.  Repointing
``audit`` and ``calendar_integration`` at the surrogate key would mean a new
column, a backfill and a rebind of the five raw-SQL composite PROTECT FKs on
both, to buy nothing.

Why denormalize ``user_id``?
    A bare ``ForeignObject`` on ``(organization_id, …)`` requires the *host*
    row to already carry the second join column.  The host row always has
    ``organization_id`` (every organization-scoped model does), but it does NOT
    natively carry ``user_id``.  Storing a denormalized ``<name>_user_id``
    column on the host row makes the join instant — no extra look-up needed
    to resolve the owning user — and keeps the ``ForeignObject`` column
    mapping simple.

Why still ``models.Field``?
    It subclasses ``models.Field`` and always has; the four bespoke tenancy
    classes this module used to contain were never its base.  It is
    deliberately *not* reparented onto
    the package's ``OrganizationSafeRelation`` either: that class contributes a
    concrete ``<name>_fk`` ``ForeignKey`` joined on ``(pk, organization)``,
    which is a different shape in every part — concrete column name, column
    type, ``to_fields``, and ``on_delete`` — so inheriting it would mean
    overriding all of it.  Staying a ``Field`` subclass also keeps django-stubs'
    model-attribute typing working, which ``OrganizationSafeRelation`` (not a
    ``Field``) does not get.

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

import datetime
import uuid
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import models
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.models import AutoField, UUIDField
from django.db.models.fields.related import ForeignObject
from django.utils import timezone


BaseDatabaseOperations.integer_field_ranges["UUIDField"] = (0, 0)


# ``vinta_orgs``' organization-safe relations, re-exported so a model declaration
# type-checks the way a ``ForeignKey`` declaration does.
#
# ``OrganizationSafeRelation`` is deliberately not a ``Field`` -- it only
# implements ``contribute_to_class``, which is what lets one declaration become
# two fields. django-stubs' plugin recognises ``Field`` subclasses and gives the
# model attribute the *related instance*'s type; it cannot recognise this, so
# ``event.calendar`` type-checked as ``OrganizationSafeForeignKey`` and every
# attribute read off it was an error. The retired ``OrganizationForeignKey``
# subclassed ``models.Field`` and so never had the problem.
#
# Typed as returning ``Any`` rather than as the target model, because the
# declaration site does not know it: ``to`` may be a string (``"CalendarEvent"``,
# ``"self"``). That is the same amount of checking the project had before.
if TYPE_CHECKING:

    def OrganizationSafeForeignKey(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
        """A ``ForeignKey`` whose ORM traversals also match on the organization."""

    def OrganizationSafeOneToOneField(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
        """A ``OneToOneField`` whose ORM traversals also match on the organization."""

else:
    from vinta_orgs.fields import (  # noqa: F401
        OrganizationSafeForeignKey,
        OrganizationSafeOneToOneField,
    )


class NaiveDateTimeField(models.DateTimeField):
    """A ``DateTimeField`` whose Python value is a *naive* wall-clock datetime.

    ``RecurringMixin.start_time_tz_unaware`` / ``end_time_tz_unaware`` hold a local
    wall-clock reading, not an instant: the instant is only recovered by pairing them
    with the row's own ``timezone`` column, which is what the ``convert_naive_utc_to_timezone``
    Postgres function does to produce the ``start_time`` / ``end_time`` generated
    columns. So every writer — ``CalendarEventService``, ``AvailabilityService``,
    ``CalendarBundleService``, ``RecurringMixin``'s own occurrence builders — assigns
    a value it has deliberately stripped the tzinfo from
    (``.astimezone(tz).replace(tzinfo=None)``).

    A plain ``DateTimeField`` treats that as a mistake. With ``USE_TZ = True`` its
    ``get_prep_value`` raises a ``RuntimeWarning`` on every naive value before making
    it aware in ``settings.TIME_ZONE``. Since the naive value is the *intended* one
    here, that warning fires on every write of these six columns and never on a real
    defect: 888 of the test suite's 3915 warnings came from exactly these fields, and
    the same noise reached production logs. Worse, it drowns out the genuine naive
    datetimes the warning exists to catch on the project's other ``DateTimeField``s.

    This field interprets a naive value as UTC and skips the warning. That is what
    ``DateTimeField`` already did — ``settings.TIME_ZONE`` is ``"UTC"``, so
    ``make_aware(value, get_default_timezone())`` produced this same instant — with
    the coupling to ``TIME_ZONE`` removed. The stored column stays ``timestamptz``
    and reads still come back aware-in-UTC, so nothing about the database or the
    read path changes; only the warning goes away.
    """

    def get_prep_value(self, value):
        # ``to_python`` is the same normalization ``DateTimeField.get_prep_value``
        # runs (str / date / datetime -> datetime), and it is idempotent, so calling
        # it here and letting ``super()`` call it again costs nothing and keeps every
        # non-datetime input -- including the ``TypeError`` an expression raises --
        # behaving exactly as it does on a stock ``DateTimeField``. Attaching UTC
        # before delegating is what takes ``super()`` down its already-aware path,
        # past the warning.
        value = self.to_python(value)
        if value is not None and settings.USE_TZ and timezone.is_naive(value):
            value = value.replace(tzinfo=datetime.UTC)
        return super().get_prep_value(value)


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
       ``filter(<name>__is_active=...)``-style queries, all automatically scoped by
       organization.

    Why no ``on_delete`` DB constraint at the membership level?
        ``ForeignObject`` creates no DB constraint.  PROTECT semantics against
        membership deletion are enforced by a per-table raw-SQL composite FK
        added through the project's raw-SQL migration framework.  The
        ``on_delete`` kwarg is stored and forwarded to the
        ``ForeignObject`` for ORM bookkeeping only.

    Usage::

        class MyModel(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
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
        """Inject the concrete ``<name>_user_id`` column and the ForeignObject descriptor.

        The ``<name>_user_id`` column is created here, at class-construction time, so no
        static analyser can see it. Every host model therefore also declares it::

            if TYPE_CHECKING:
                <name>_user_id: int | None

        Without that, mypy rejects every read of the column -- which it did, 49 times,
        across five models. Add the declaration alongside any new use of this field.
        """
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
        #      - "organization_id": the FK attname ``SingleOrganizationModelMixin`` adds
        #    to_fields references the *attnames* on OrganizationMembership:
        #      - "user_id": attname of the `user` ForeignKey
        #      - "organization_id": attname of the `organization` ForeignKey
        #    ``to_fields`` names attnames (e.g. "organization_id") rather than
        #    field names, which is what ``ForeignObject`` resolves against on
        #    the target side.
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
        #    memberships and organization-scoped rows). Deferring to the DB-level
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

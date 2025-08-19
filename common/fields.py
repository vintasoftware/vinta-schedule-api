import uuid

from django.db import models
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.models import AutoField, UUIDField
from django.db.models.fields.related import ForeignObject


BaseDatabaseOperations.integer_field_ranges["UUIDField"] = (0, 0)


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

"""Give every organization the scope that bills it, before ``vinta_billing`` tries to.

``vinta_billing.0005_backfill_scopes`` is the package's own 0.7-to-0.8 backfill:
it makes one scope per organization and re-points every billing row at it. It
can only do that through the shipped scope's generic key
(``content_type``/``object_id``). This project's scope has a real foreign key
instead, so ``0005`` cannot name a payer here and says so rather than guessing::

    RuntimeError: vinta_billing has 4 organization(s) to migrate onto scopes,
    but BILLING_SCOPE_MODEL points at a scope model with no generic key [...]
    Backfill those scopes in your own data migration and point the billing rows
    at them, then re-run with this one faked.

This is that data migration. It runs *before* ``0005`` so the rows are already
scoped by the time the package looks.

Three things happen here that ``0005`` would not have done anyway:

**A scope for every organization, not only the ones with billing rows.**
``payments.seams.scopes`` provisions a scope on every organization ``post_save``
and every reader assumes one exists, so leaving the organizations that happen to
have no billing row yet without one would make this migration's output differ
from what the running system maintains.

**The tree comes with it.** ``parent`` and ``is_reseller_root`` mirror
``Organization.parent`` and ``Organization.can_invite_organizations``. Without
them ``ResellerHierarchy`` sees a flat table, every scope becomes its own
billing root, and a reseller's children stop pooling usage into the reseller's
ceiling -- the counts simply come out wrong, in the direction of under-charging
the paying root. Nothing raises; it is found in a reconciliation months later.

**Usage breakdowns keep every key.** ``BillingPeriodResourceUsage`` is keyed
``{organization_id: count}`` and the reseller's row names its *children* --
which are exactly the organizations that tend to have no billing rows of their
own. ``0005`` maps only the organizations it made scopes for and drops the rest,
so the per-child breakdown behind a reseller's bill would come out empty. Since
every organization gets a scope above, every key survives.

Idempotent: re-running resolves to the same scopes and rewrites nothing that is
already right. Reversible: the reverse reads the organization back off each
scope, restores the columns, and empties the table.
"""

from django.db import migrations
from django.db.models import OuterRef, Subquery


#: The five tables ``vinta_billing`` re-keys from ``organization`` onto ``scope``.
#: Spelled out rather than imported from the package: a migration is a
#: historical record and must not change meaning when a later release edits its
#: own list.
SCOPED_MODELS = (
    "BillingProfile",
    "Subscription",
    "PaymentMethod",
    "MeteredOccurrence",
    "BillingPeriodSummary",
)

#: ``BillingProfile.organization_id`` is the table's primary key until
#: ``vinta_billing.0006`` renames it to ``id``, so unlike the other four it
#: cannot be cleared here. Leaving it set is harmless -- ``0006`` is what
#: retires the column, and this migration only has to stop *reading* it.
KEEPS_ITS_ORGANIZATION_COLUMN = frozenset({"BillingProfile"})


def _scope_model(apps):
    return apps.get_model("billing_integration", "OrganizationBillingScope")


def _organization_model(apps, Scope):
    """The historical organization model, read off the foreign key.

    Taken from the field rather than written out so this follows
    ``ORGANIZATION_MODEL`` if it is ever pointed elsewhere -- the same reason
    ``OrganizationBillingScope.build_scope_key`` derives its label.
    """
    return Scope._meta.get_field("organization").related_model


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Scope = _scope_model(apps)
    Organization = _organization_model(apps, Scope)

    organizations = list(
        Organization.objects.using(db)
        .values_list("id", "parent_id", "can_invite_organizations", "name")
        .order_by("id")
    )
    if not organizations:
        # A database built from zero. There is nothing to name, and `0005` will
        # find nothing either.
        return

    label_prefix = Organization._meta.label_lower
    scope_id_by_organization = dict(
        Scope.objects.using(db).values_list("organization_id", "id")
    )

    missing = [
        Scope(
            organization_id=organization_id,
            scope_type="organization",
            scope_key=f"{label_prefix}:{organization_id}",
            label=name[:255],
            meta={},
            is_reseller_root=bool(can_invite),
        )
        for organization_id, _parent_id, can_invite, name in organizations
        if organization_id not in scope_id_by_organization
    ]
    if missing:
        Scope.objects.using(db).bulk_create(missing, batch_size=500)
        scope_id_by_organization = dict(
            Scope.objects.using(db).values_list("organization_id", "id")
        )

    # Second pass for `parent`: a scope's parent scope has to exist before it
    # can be pointed at, and the organizations above are in primary-key order,
    # which says nothing about tree order.
    wanted = {
        organization_id: (
            scope_id_by_organization.get(parent_id) if parent_id is not None else None,
            bool(can_invite),
            name[:255],
        )
        for organization_id, parent_id, can_invite, name in organizations
    }
    stale = []
    for scope in Scope.objects.using(db).iterator():
        target = wanted.get(scope.organization_id)
        if target is None:
            continue
        parent_id, is_root, label = target
        if (scope.parent_id, scope.is_reseller_root, scope.label) == (parent_id, is_root, label):
            continue
        scope.parent_id = parent_id
        scope.is_reseller_root = is_root
        scope.label = label
        stale.append(scope)
    if stale:
        Scope.objects.using(db).bulk_update(
            stale, ["parent", "is_reseller_root", "label"], batch_size=500
        )

    # One statement per table rather than one per organization: the join is
    # what the database is for, and an installation with a few hundred tenants
    # would otherwise run a few thousand updates inside the deploy's migrate.
    scope_of_this_row = Subquery(
        Scope.objects.filter(organization_id=OuterRef("organization_id")).values("id")[:1]
    )
    for model_name in SCOPED_MODELS:
        model = apps.get_model("vinta_billing", model_name)
        model.objects.using(db).filter(
            organization_id__isnull=False, scope_id__isnull=True
        ).update(scope_id=scope_of_this_row)

        if model_name not in KEEPS_ITS_ORGANIZATION_COLUMN:
            model.objects.using(db).filter(scope_id__isnull=False).update(organization_id=None)

    _rekey_usage_breakdowns(apps, db, scope_id_by_organization)


def _rekey_usage_breakdowns(apps, db, scope_id_by_organization):
    """``{organization_id: count}`` becomes ``{scope_id: count}``.

    Both sides are strings: these are JSON object keys, and JSON has no other
    kind.
    """
    Usage = apps.get_model("vinta_billing", "BillingPeriodResourceUsage")

    for usage in Usage.objects.using(db).all().iterator():
        by_organization = usage.by_organization or {}
        if not by_organization or usage.by_scope:
            continue
        usage.by_scope = {
            str(scope_id_by_organization[int(key)]): count
            for key, count in by_organization.items()
            if int(key) in scope_id_by_organization
        }
        usage.by_organization = {}
        usage.save(update_fields=["by_scope", "by_organization"])


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Scope = _scope_model(apps)

    organization_by_scope = dict(Scope.objects.using(db).values_list("id", "organization_id"))
    if not organization_by_scope:
        return

    Usage = apps.get_model("vinta_billing", "BillingPeriodResourceUsage")
    for usage in Usage.objects.using(db).all().iterator():
        by_scope = usage.by_scope or {}
        if not by_scope:
            continue
        usage.by_organization = {
            str(organization_by_scope[int(key)]): count
            for key, count in by_scope.items()
            if int(key) in organization_by_scope
        }
        usage.by_scope = {}
        usage.save(update_fields=["by_scope", "by_organization"])

    organization_of_this_row = Subquery(
        Scope.objects.filter(id=OuterRef("scope_id")).values("organization_id")[:1]
    )
    for model_name in SCOPED_MODELS:
        model = apps.get_model("vinta_billing", model_name)
        if model_name not in KEEPS_ITS_ORGANIZATION_COLUMN:
            model.objects.using(db).filter(scope_id__isnull=False).update(
                organization_id=organization_of_this_row
            )
        model.objects.using(db).filter(scope_id__isnull=False).update(scope_id=None)

    # `parent` is PROTECT, so the self-references have to go before the rows do.
    Scope.objects.using(db).update(parent=None)
    Scope.objects.using(db).all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing_integration", "0001_initial"),
        # The `scope_id` columns this fills are added by `0004`.
        ("vinta_billing", "0004_add_scope_columns"),
    ]

    # Before, not after: `0005` refuses to run while any billing row still
    # carries an organization it cannot translate, and the whole point of this
    # migration is that the rows are already scoped when it looks.
    run_before = [
        ("vinta_billing", "0005_backfill_scopes"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

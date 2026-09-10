"""Mirror the organization tree onto the billing scopes ``vinta_billing`` created.

``vinta_billing``'s ``0005_backfill_scopes`` makes one ``BillingScope`` per
organization and re-points every billing row at it, which is everything the
package can do on its own. What it cannot know is that this project's
organizations form a *tree*: it leaves ``parent`` NULL on every scope it
creates, and it has nowhere to put ``Organization.can_invite_organizations``.

Left that way, ``payments.seams.hierarchy.ResellerHierarchy`` sees a flat table.
Every scope becomes its own billing root, so a reseller's children stop pooling
their usage into the reseller's ceiling and each starts looking for a
subscription of its own. Nothing raises -- the counts simply come out wrong, in
the direction of under-charging the paying root, which is the kind of billing
bug that is found in a quarterly reconciliation rather than in an error report.

This migration closes that gap once, for rows that already exist.
``payments.seams.scopes`` keeps the mirror in step from here on, off
``post_save``.

Reversible, and genuinely so: the reverse clears exactly the two things the
forward sets, returning the scope table to the state ``0005`` left it in. It
does not delete scopes -- those are ``vinta_billing``'s to make and unmake, and
``0005``'s own reverse handles them.
"""

from django.db import migrations


#: Must match ``payments.seams.hierarchy.RESELLER_ROOT_META_KEY``. Spelled out
#: rather than imported: a migration is a historical record and must not change
#: meaning when the constant it referenced is renamed years later.
RESELLER_ROOT_META_KEY = "is_reseller_root"


def shipped_scope_table_exists(schema_editor) -> bool:
    """Whether ``vinta_billing``'s own scope table is present.

    It is not, on any database built after ``BILLING_SCOPE_MODEL`` was pointed
    at ``billing_integration.OrganizationBillingScope``: ``vinta_billing``'s
    ``0003`` creates ``BillingScope`` as swappable, so Django skips it and only
    the project's model gets a table.

    Both directions of this migration read that table, so both ask first. An
    installation that predates the swap still has it, still has rows in it, and
    still needs the mirror -- ``billing_integration.0002`` copies what this
    writes onto the project's own model in the same deploy.
    """
    return "vinta_billing_billingscope" in schema_editor.connection.introspection.table_names()


def _scope_by_organization_id(apps):
    """``{organization_id: scope}`` for every scope naming an organization.

    Keyed off ``object_id``, the generic key ``0005`` stamped -- a string
    column, since a scope may name something whose primary key is not an
    integer, so it is cast back here. Scopes naming anything else (a personal
    plan, in a project that grows one) are skipped rather than guessed at.
    """
    BillingScope = apps.get_model("vinta_billing", "BillingScope")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type = ContentType.objects.filter(app_label="organizations", model="organization").first()
    if content_type is None:
        return {}

    mapping = {}
    for scope in BillingScope.objects.filter(content_type=content_type):
        try:
            mapping[int(scope.object_id)] = scope
        except (TypeError, ValueError):
            continue
    return mapping


def forwards(apps, schema_editor):
    if not shipped_scope_table_exists(schema_editor):
        return

    Organization = apps.get_model("organizations", "Organization")
    BillingScope = apps.get_model("vinta_billing", "BillingScope")

    scopes = _scope_by_organization_id(apps)
    if not scopes:
        return

    organizations = Organization.objects.values_list("id", "parent_id", "can_invite_organizations")

    updated = []
    for organization_id, parent_id, can_invite in organizations:
        scope = scopes.get(organization_id)
        if scope is None:
            # An organization with no billing rows at 0.7 never got a scope from
            # 0005. It gets one from `payments.seams.scopes` the first time it
            # is saved or asked a billing question; there is nothing to mirror
            # onto here.
            continue

        parent_scope = scopes.get(parent_id) if parent_id is not None else None
        meta = dict(scope.meta or {})
        meta[RESELLER_ROOT_META_KEY] = bool(can_invite)

        scope.parent = parent_scope
        scope.meta = meta
        updated.append(scope)

    # One statement rather than a save per row: this runs inside the deploy's
    # migrate step, and the scope table is one row per organization.
    if updated:
        BillingScope.objects.bulk_update(updated, ["parent", "meta"], batch_size=500)


def backwards(apps, schema_editor):
    if not shipped_scope_table_exists(schema_editor):
        return

    """Undo exactly what ``forwards`` set, and nothing else."""
    BillingScope = apps.get_model("vinta_billing", "BillingScope")

    scopes = list(_scope_by_organization_id(apps).values())
    if not scopes:
        return

    for scope in scopes:
        meta = dict(scope.meta or {})
        meta.pop(RESELLER_ROOT_META_KEY, None)
        scope.parent = None
        scope.meta = meta

    BillingScope.objects.bulk_update(scopes, ["parent", "meta"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0025_rename_calendar_groups_resource_key"),
        # The scope rows this reads must exist and must already be final:
        # 0005 creates and populates them, 0006 drops the organization columns
        # they replaced. Depending on 0006 rather than 0005 keeps this after the
        # whole upgrade sequence rather than in the middle of it.
        ("vinta_billing", "0006_drop_organization_columns"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

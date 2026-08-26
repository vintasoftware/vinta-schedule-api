"""Move the rows from the legacy ``audit`` app's table into the new log.

The old ``audit`` app kept one wide, organization-scoped table where the actor,
the action and the scope were all columns. ``vinta_audit_logs`` splits those into
dimensions that the log points at, so this is not a schema alteration -- it is a
read of the old table and a write of the new ones. The legacy table is left
exactly as it was; dropping it is a separate, deliberate step once the new log
has been checked.

Reads go through raw SQL rather than ``apps.get_model``. The ``audit`` app is no
longer installed and its migrations are gone, so it does not exist in the
migration state at all -- but its table is still sitting in the database, and
that is all this needs.

Safety properties, in the order they matter:

* **Idempotent.** Every legacy row already carries the ``uid`` that is this
  record's identity in every repository. A row whose uid is already in the new
  table is skipped, so re-running after a partial failure resumes rather than
  duplicates.
* **Batched.** Rows are walked by keyset on the legacy primary key, in batches,
  each batch its own set of statements. Nothing holds a lock across the whole
  table and nothing loads the whole table into memory.
* **Absent-table tolerant.** A database that never had the ``audit`` app -- a
  fresh install, a test database -- skips the whole thing.

The reverse is a no-op on purpose. Deleting audit records to undo a migration is
not a thing this project should be able to do by accident; the legacy table is
still there, so a genuine rollback means pointing back at it.
"""

import json

from django.conf import settings
from django.db import migrations


#: Legacy rows read per round trip. The new-row writes fan out to roughly four
#: inserts per legacy row (scope and action are deduplicated, identity and audit
#: are not), so this stays small enough that one batch is a short transaction.
BATCH_SIZE = 500

LEGACY_AUDIT_TABLE = "audit_audit"
LEGACY_AFFECTED_TABLE = "audit_auditaffectedmembership"

#: Legacy ``actor_type`` values map onto ``audit_integration`` identity types
#: unchanged -- the vocabulary was carried over deliberately so the log reads the
#: same on both sides of the move.
SCOPE_TYPE_SCOPED = "scoped"
SCOPE_TYPE_GLOBAL = "global"


def _table_exists(cursor, table_name: str) -> bool:
    """Whether a table is present in the database this migration is running on."""
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [table_name])
    return bool(cursor.fetchone()[0])


def _normalize_subject_type(subject_type: str) -> str:
    """Rewrite ``"app_label.ModelName"`` as ``"app_label.modelname"``.

    The legacy service built the subject type from ``__class__.__name__``; the
    new one builds it from ``Meta.model_name``, which is lower-cased. Normalizing
    on the way in means a filter by subject type finds records from both eras
    instead of silently splitting the history in two.
    """
    if not subject_type or "." not in subject_type:
        return subject_type or ""
    app_label, _, model_name = subject_type.partition(".")
    return f"{app_label}.{model_name.lower()}"


#: Legacy columns holding JSON. Read through a raw cursor they arrive as *text*,
#: not as Python objects: Django's psycopg3 backend deliberately leaves JSON
#: undecoded and lets ``JSONField.from_db_value`` do it, which never runs here
#: because there is no model to run it. Left alone, ``system_user_scopes`` would
#: land in the new table as the string ``'["calendars"]'`` -- iterable, so
#: nothing would raise, and the list would arrive as thirteen single characters.
_JSON_COLUMNS = ("system_user_scopes", "diff")


def _decode_json_columns(row: dict) -> dict:
    """Turn the JSON columns of one raw row into Python objects.

    Tolerant of a value that is already decoded, so this keeps working if the
    backend's JSON handling changes underneath it.
    """
    for column in _JSON_COLUMNS:
        value = row.get(column)
        if isinstance(value, str):
            try:
                row[column] = json.loads(value)
            except (TypeError, ValueError):
                # Unparseable JSON in a legacy row is worth neither guessing at
                # nor aborting the whole move for. The rest of the record is
                # intact and is the part that matters.
                row[column] = None
    return row


def _identity_fields(row: dict, existing_user_ids: set) -> dict:
    """Build the identity row for one legacy audit record's actor.

    One identity row per record, matching how the new log works: the snapshot
    describes the actor at one moment, so it is not shared between records.

    The project columns and ``metadata`` are both filled, exactly as
    ``OrganizationAuditRepository.build_identity_defaults`` does at runtime --
    the columns are what this database queries on, and ``metadata`` is what a
    replica with the stock identity model would receive.
    """
    actor_type = row["actor_type"] or "system"
    actor_id = row["actor_id"]
    role = row["actor_role"] or ""
    scopes = row["system_user_scopes"] or []
    scoped_to = row["system_user_scoped_to_membership"]

    metadata: dict = {}
    if role:
        # The old schema stored a derived *role label* ("admin" / "member") and
        # nothing else about what a membership could do. The new one stores the
        # groups and permissions themselves -- which these rows simply do not
        # have, and which cannot be reconstructed: the membership's groups today
        # are not the groups it held when the action happened, and pretending
        # otherwise would be inventing audit data.
        #
        # So the label is carried across as what it is, under its own key, and
        # ``group_names`` / ``permission_keys`` stay empty for pre-migration
        # records. Anything reading the trail across the boundary needs to know
        # that: before the move there is a role, after it there are groups.
        metadata["legacy_membership_role"] = role
    if scopes:
        metadata["system_user_scopes"] = list(scopes)
    if scoped_to is not None:
        metadata["system_user_scoped_to_membership"] = scoped_to

    return {
        "identity_type": actor_type,
        "identity_key": "" if actor_id is None else str(actor_id),
        "identity_label": "",
        # Only a membership actor is a user, and only if that user is still
        # there. A system-user id is an API token's and a single-use code id is a
        # token row's -- pointing ``user`` at either would claim a person acted
        # when one did not.
        #
        # The existence check is not defensive tidiness: ``user`` is a real
        # foreign key on the new identity model where the legacy table had a bare
        # integer, so a single actor whose account has since been deleted would
        # otherwise fail the insert and take the whole migration down with it.
        # The account being gone is exactly the situation an audit trail is meant
        # to survive; ``identity_key`` still records who it was.
        "user_id": (
            actor_id if actor_type == "membership" and actor_id in existing_user_ids else None
        ),
        "is_staff": False,
        "is_superuser": False,
        "group_names": [],
        "permission_keys": [],
        "metadata": metadata,
        "system_user_scopes": list(scopes),
        "system_user_scoped_to_membership": scoped_to,
    }


def backfill(apps, schema_editor):
    """Copy every legacy audit row into the new log, in batches."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        if not _table_exists(cursor, LEGACY_AUDIT_TABLE):
            return
        has_affected = _table_exists(cursor, LEGACY_AFFECTED_TABLE)

    Audit = apps.get_model("vinta_audit_logs", "Audit")
    AuditAction = apps.get_model("vinta_audit_logs", "AuditAction")
    AuditAffectedIdentity = apps.get_model("vinta_audit_logs", "AuditAffectedIdentity")
    Scope = apps.get_model("audit_integration", "OrganizationAuditScope")
    Identity = apps.get_model("audit_integration", "OrganizationAuditIdentity")
    ContentType = apps.get_model("contenttypes", "ContentType")
    # Resolved through the swappable setting, the way a migration must: the user
    # model this project runs is not necessarily the one this migration was
    # written against.
    User = apps.get_model(settings.AUTH_USER_MODEL)

    # Caches for the two deduplicated dimensions and for content types. All three
    # are small and bounded -- one scope per organization, a few dozen actions,
    # one content type per audited model.
    scope_ids: dict[str, int] = {}
    action_ids: dict[str, int] = {}
    content_type_ids: dict[str, int | None] = {}

    def scope_id_for(organization_id: int | None) -> int:
        key = "" if organization_id is None else str(organization_id)
        if key in scope_ids:
            return scope_ids[key]
        # Historical models carry no custom save(), so scope_key is set by hand
        # here rather than derived by AbstractAuditScope.save().
        scope, _created = Scope.objects.get_or_create(
            scope_type=SCOPE_TYPE_GLOBAL if organization_id is None else SCOPE_TYPE_SCOPED,
            scope_key=key,
            defaults={
                "organization_id": organization_id,
                "label": "" if organization_id is None else f"Organization {organization_id}",
            },
        )
        scope_ids[key] = scope.pk
        return scope.pk

    def action_id_for(action_key: str) -> int:
        if action_key in action_ids:
            return action_ids[action_key]
        action, _created = AuditAction.objects.get_or_create(
            content_type_key="",
            key=action_key,
            defaults={"name": action_key},
        )
        action_ids[action_key] = action.pk
        return action.pk

    def content_type_id_for(subject_type: str) -> int | None:
        if subject_type in content_type_ids:
            return content_type_ids[subject_type]
        content_type_id = None
        if "." in subject_type:
            app_label, _, model_name = subject_type.partition(".")
            match = ContentType.objects.filter(app_label=app_label, model=model_name).first()
            content_type_id = match.pk if match is not None else None
        content_type_ids[subject_type] = content_type_id
        return content_type_id

    columns = (
        "id",
        "uid",
        "organization_id",
        "created_at",
        "action",
        "actor_type",
        "actor_id",
        "actor_role",
        "system_user_scopes",
        "system_user_scoped_to_membership",
        "subject_type",
        "subject_id",
        "subject_label",
        "diff",
    )
    select = (
        f"SELECT {', '.join(columns)} FROM {LEGACY_AUDIT_TABLE} "  # noqa: S608 - fixed identifiers
        "WHERE id > %s ORDER BY id LIMIT %s"
    )

    last_id = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(select, [last_id, BATCH_SIZE])
            rows = [
                _decode_json_columns(dict(zip(columns, values, strict=True)))
                for values in cursor.fetchall()
            ]
        if not rows:
            return
        last_id = rows[-1]["id"]

        # Skip what a previous run already moved. The uid is the record's
        # identity in every repository, so its presence means this row is done.
        existing_uids = set(
            Audit.objects.filter(uid__in=[row["uid"] for row in rows]).values_list(
                "uid", flat=True
            )
        )
        pending = [row for row in rows if row["uid"] not in existing_uids]
        if not pending:
            continue

        # Which of this batch's membership actors still have a user row. One
        # query for the batch, because ``user`` is a real foreign key now and a
        # deleted account would otherwise abort the whole migration.
        candidate_user_ids = {
            row["actor_id"]
            for row in pending
            if row["actor_type"] == "membership" and row["actor_id"] is not None
        }
        existing_user_ids = (
            set(
                User.objects.filter(pk__in=candidate_user_ids).values_list("pk", flat=True)
            )
            if candidate_user_ids
            else set()
        )

        # Actor identities: one row per record, created in one statement.
        actors = Identity.objects.bulk_create(
            [Identity(**_identity_fields(row, existing_user_ids)) for row in pending]
        )

        audits = []
        for row, actor in zip(pending, actors, strict=True):
            subject_type = _normalize_subject_type(row["subject_type"])
            audits.append(
                Audit(
                    uid=row["uid"],
                    created_at=row["created_at"],
                    action_id=action_id_for(row["action"]),
                    action_key=row["action"],
                    scope_id=scope_id_for(row["organization_id"]),
                    scope_type=(
                        SCOPE_TYPE_GLOBAL if row["organization_id"] is None else SCOPE_TYPE_SCOPED
                    ),
                    scope_key="" if row["organization_id"] is None else str(row["organization_id"]),
                    actor_id=actor.pk,
                    # Denormalized copies of the actor's identity, so filtering
                    # the log by actor needs no join -- see the Audit model.
                    actor_type=actor.identity_type,
                    actor_key=actor.identity_key,
                    on_behalf_of_id=None,
                    subject_content_type_id=content_type_id_for(subject_type),
                    subject_content_type_key=subject_type,
                    subject_pk=row["subject_id"],
                    subject_label=row["subject_label"] or "",
                    diff=row["diff"] or None,
                )
            )
        Audit.objects.bulk_create(audits)

        if not has_affected:
            continue

        # The affected memberships of every record in this batch, in one query.
        legacy_ids = [row["id"] for row in pending]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit_fk_id, membership_user_id "  # noqa: S608 - fixed identifiers
                f"FROM {LEGACY_AFFECTED_TABLE} WHERE audit_fk_id = ANY(%s)",
                [legacy_ids],
            )
            affected_rows = cursor.fetchall()
        if not affected_rows:
            continue

        audit_by_legacy_id = dict(zip(legacy_ids, audits, strict=True))
        # Same existence check as the actors above, and for the same reason: an
        # affected party whose account has since been deleted must not abort the
        # migration. ``identity_key`` still records who they were.
        existing_affected_user_ids = set(
            User.objects.filter(
                pk__in={membership_user_id for _fk, membership_user_id in affected_rows}
            ).values_list("pk", flat=True)
        )
        # An affected party is a snapshot too, so each link gets its own identity
        # row rather than sharing the actor's.
        affected_identities = Identity.objects.bulk_create(
            [
                Identity(
                    identity_type="membership",
                    identity_key=str(membership_user_id),
                    user_id=(
                        membership_user_id
                        if membership_user_id in existing_affected_user_ids
                        else None
                    ),
                    metadata={},
                    group_names=[],
                    permission_keys=[],
                    system_user_scopes=[],
                )
                for _audit_fk_id, membership_user_id in affected_rows
            ]
        )
        AuditAffectedIdentity.objects.bulk_create(
            [
                AuditAffectedIdentity(
                    audit_id=audit_by_legacy_id[audit_fk_id].pk,
                    identity_id=identity.pk,
                    # Denormalized so "every record that touched this person" is
                    # answered from the link table alone -- see the model.
                    identity_type="membership",
                    identity_key=str(membership_user_id),
                )
                for (audit_fk_id, membership_user_id), identity in zip(
                    affected_rows, affected_identities, strict=True
                )
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("audit_integration", "0001_initial"),
        ("vinta_audit_logs", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

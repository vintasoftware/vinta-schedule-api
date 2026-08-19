"""Repair recurring parents whose bulk-modification truncation never reached the database.

See README.md in this folder for run / monitor / interrupt / restore commands.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Min

from dateutil.rrule import rrulestr

from calendar_integration.models import (
    AvailableTime,
    AvailableTimeBulkModification,
    BlockedTime,
    BlockedTimeBulkModification,
    CalendarEvent,
    EventBulkModification,
    RecurrenceRule,
)
from payments.models import MeteredOccurrence, Subscription
from payments.services.billing_dataclasses import OccurrenceIdentity
from payments.services.subscription_service import resolve_settlement_period
from scripts.one_off._base import BaseOneOffScript, ScriptConfig


SCRIPT_NAME = "2026-08-05-repair-untruncated-recurring-parents"

RULE_TABLE = "calendar_integration_recurrencerule"
# Read off the model rather than written as a literal: the billing engine moved to
# ``vinta-django-billing`` in ``payments/migrations/0024_move_billing_to_vinta_billing.py``,
# which renamed this table to ``vinta_billing_meteredoccurrence``. The script already
# imports the live model (it is a one-off, not a migration), so deriving the name keeps
# it re-runnable instead of pinning it to a table that no longer exists.
METERED_TABLE = MeteredOccurrence._meta.db_table

# How far a series-root walk may climb before giving up. Mirrors
# ``MeteringService.MAX_SERIES_CHAIN_DEPTH`` -- ``bulk_modification_parent`` is
# ordinary mutable data and a cycle would otherwise loop forever.
MAX_SERIES_CHAIN_DEPTH = 10


@dataclass(frozen=True)
class _Kind:
    """One of the three recurring object types a bulk modification can split."""

    key: str
    # Typed `Any` rather than `type[Model]`: both are read through
    # `original_manager`, which the organization-scoping machinery attaches
    # dynamically and mypy therefore cannot see on a `type` annotation.
    parent_model: Any
    record_model: Any
    parent_field: str
    is_metered: bool


KINDS: tuple[_Kind, ...] = (
    _Kind("event", CalendarEvent, EventBulkModification, "parent_event", True),
    _Kind("blocked_time", BlockedTime, BlockedTimeBulkModification, "parent_blocked_time", False),
    _Kind(
        "available_time",
        AvailableTime,
        AvailableTimeBulkModification,
        "parent_available_time",
        False,
    ),
)
KIND_BY_KEY = {kind.key: kind for kind in KINDS}


@dataclass(frozen=True)
class RepairTarget:
    """One parent series that needs its truncation written back.

    ``expected_until`` is already computed and validated by ``iter_targets`` --
    only actionable targets are yielded, so ``process`` never has to re-decide
    whether a repair is safe.
    """

    kind: str
    parent_id: int
    organization_id: int
    first_modification_start: datetime.datetime
    expected_until: datetime.datetime
    reason: str


class RepairUntruncatedRecurringParents(BaseOneOffScript[RepairTarget]):
    """Repair parents left un-truncated by the recurrence bulk-modification defect."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Counters for the classifications that are reported rather than repaired.
        self.reported: dict[str, int] = {}
        self._expected_identity_cache: dict[
            tuple[int, datetime.datetime], set[OccurrenceIdentity]
        ] = {}
        self._metered_deleted = 0
        self._metered_in_closed_periods = 0

    def describe(self) -> str:
        return (
            "Repairs recurring series whose parent was never truncated when a bulk "
            "modification split them. Two upstream defects (both fixed 2026-08-05) left "
            "the parent's RecurrenceRule row without its UNTIL: the splitter returned "
            "copy.deepcopy clones that shared the original row's pk so the continuation's "
            "save() overwrote the parent's truncation, and on the AvailableTime / "
            "BlockedTime path the truncation was never written at all. An open-ended "
            "parent therefore never stopped and duplicated its series indefinitely. For "
            "each parent carrying a bulk-modification record this script recomputes the "
            "correct boundary -- the last occurrence strictly before the earliest "
            "modification start -- and writes it back as UNTIL with COUNT cleared. For "
            "CalendarEvent series it then deletes the MeteredOccurrence rows that were "
            "billed for phantom occurrences the repaired series no longer generates."
        )

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def iter_targets(self) -> Iterator[RepairTarget]:
        for kind in KINDS:
            yield from self._iter_kind(kind)

    def _iter_kind(self, kind: _Kind) -> Iterator[RepairTarget]:
        """Keyset-paginate the bulk-modification records grouped by parent.

        Grouped so a parent split more than once is repaired against its *earliest*
        modification start -- that is the point the parent must stop at, regardless
        of how many later splits followed. Keyset rather than OFFSET so the scan
        stays linear on a large table.
        """
        # ``parent_event`` and friends are virtual ForeignObjects; the concrete
        # column is the ``_fk`` twin the OrganizationForeignKey machinery generates.
        fk_field = f"{kind.parent_field}_fk"
        id_field = f"{fk_field}_id"
        last_seen = 0
        while True:
            rows = list(
                kind.record_model.original_manager.filter(
                    **{f"{fk_field}__isnull": False, f"{id_field}__gt": last_seen}
                )
                .values(id_field)
                .annotate(first_mod=Min("modification_start_date"))
                .order_by(id_field)[: self.config.batch_size]
            )
            if not rows:
                return

            first_mod_by_id = {row[id_field]: row["first_mod"] for row in rows}
            last_seen = rows[-1][id_field]

            parents = kind.parent_model.original_manager.filter(
                pk__in=list(first_mod_by_id)
            ).select_related("recurrence_rule_fk")
            for parent in parents:
                target = self._classify(kind, parent, first_mod_by_id[parent.pk])
                if target is not None:
                    yield target

    def _classify(
        self, kind: _Kind, parent: Any, first_mod: datetime.datetime
    ) -> RepairTarget | None:
        """Decide whether ``parent`` is unambiguously corrupt, and skip it if not.

        Only a parent whose rule is *provably* missing its truncation is repaired.
        A rule carrying some other UNTIL may be a legitimate later edit, and
        overwriting it would destroy real data to fix a bug that may not be there.
        Those are counted and logged for manual review instead.
        """
        rule: RecurrenceRule | None = parent.recurrence_rule
        if rule is None:
            # Split at the first occurrence leaves the parent non-recurring, and
            # that path always persisted correctly. Nothing to repair.
            self._report("no-recurrence-rule", kind, parent)
            return None

        expected_until = self._last_occurrence_before(rule, parent.start_time, first_mod)
        if expected_until is None:
            self._report("no-occurrence-before-split", kind, parent)
            return None

        if rule.until is None:
            reason = "corrupt-unbounded"
        elif rule.until == expected_until:
            if rule.count is None:
                self._report("already-correct", kind, parent)
                return None
            reason = "corrupt-count-set"
        else:
            self._report("ambiguous-until-mismatch", kind, parent)
            return None

        return RepairTarget(
            kind=kind.key,
            parent_id=parent.pk,
            organization_id=parent.organization_id,
            first_modification_start=first_mod,
            expected_until=expected_until,
            reason=reason,
        )

    def _report(self, reason: str, kind: _Kind, parent: Any) -> None:
        self.reported[reason] = self.reported.get(reason, 0) + 1
        if reason in ("ambiguous-until-mismatch", "no-occurrence-before-split"):
            # The two cases a human has to look at. Log every one with enough
            # identity to find it; the others are ordinary no-ops.
            self.runtime.log(
                "WARN",
                f"needs review [{reason}] {kind.key} id={parent.pk} "
                f"org={parent.organization_id} until={getattr(parent.recurrence_rule, 'until', None)}",
            )

    @staticmethod
    def _last_occurrence_before(
        rule: RecurrenceRule, dtstart: datetime.datetime, split: datetime.datetime
    ) -> datetime.datetime | None:
        """Last occurrence strictly before ``split``, ignoring the rule's bounds.

        COUNT and UNTIL are dropped from the pattern on purpose: those are exactly
        the two columns the defect corrupted, so honouring them here would compute
        the boundary from the damage. The cadence fields are untouched by the bug.
        """
        parts = [
            part
            for part in rule.to_rrule_string().split(";")
            if not part.startswith(("COUNT=", "UNTIL="))
        ]
        return rrulestr("RRULE:" + ";".join(parts), dtstart=dtstart).before(split, inc=False)

    def item_id(self, item: RepairTarget) -> str:
        return f"{item.kind}:{item.parent_id}"

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def tables_touched(self) -> list[str]:
        return [RULE_TABLE, METERED_TABLE]

    def snapshot(self, item: RepairTarget) -> dict[str, dict[str, Any]]:
        """Back up the rule row this item is about to overwrite.

        Deleted ``MeteredOccurrence`` rows are backed up separately inside
        ``process`` -- there are many per item and ``snapshot`` is one row per
        table by construction.
        """
        kind = KIND_BY_KEY[item.kind]
        parent = kind.parent_model.original_manager.select_related("recurrence_rule_fk").get(
            pk=item.parent_id
        )
        rule = parent.recurrence_rule
        return {
            RULE_TABLE: {
                "id": rule.pk,
                "organization_id": rule.organization_id,
                "count": rule.count if rule.count is not None else "",
                "until": rule.until.isoformat() if rule.until else "",
            }
        }

    # ------------------------------------------------------------------
    # Per-item action
    # ------------------------------------------------------------------

    def process(self, item: RepairTarget) -> None:
        kind = KIND_BY_KEY[item.kind]
        # No `organization_context` binding here: every access below -- this
        # method's own `kind.parent_model.original_manager` call and
        # `_delete_phantom_metered`'s `original_manager` calls -- deliberately
        # bypasses tenant scoping, mirroring the scan in
        # `iter_targets`/`_iter_kind`, which is cross-organization by design
        # (same pattern as `organizations/admin.py`'s explicit
        # `original_manager` usage). `_delete_phantom_metered` also reads
        # `Subscription`/`MeteredOccurrence` via `.objects`, but both are
        # plain `BaseModel` (not organization-scoped -- see `payments/tasks
        # .py`'s module docstring), so those calls need no organization
        # context either way.
        with transaction.atomic():
            parent = (
                kind.parent_model.original_manager.select_related("recurrence_rule_fk")
                .select_for_update(of=("self",))
                .get(pk=item.parent_id)
            )
            rule = parent.recurrence_rule
            if rule is None:
                self.runtime.log(
                    "WARN",
                    f"{self.item_id(item)}: rule disappeared between scan and write, skipping",
                )
                return

            rule.count = None
            rule.until = item.expected_until
            rule.save(update_fields=["count", "until"])
            self.runtime.log(
                "INFO",
                f"{self.item_id(item)}: [{item.reason}] truncated at {item.expected_until.isoformat()}",
            )

            if kind.is_metered:
                self._delete_phantom_metered(parent, item)

    # ------------------------------------------------------------------
    # Metering cleanup (CalendarEvent only -- the other two are not billed)
    # ------------------------------------------------------------------

    def _delete_phantom_metered(self, parent: CalendarEvent, item: RepairTarget) -> None:
        """Delete metered rows for occurrences the repaired series no longer generates.

        Two guards keep this narrow, and both matter:

        - **Only past ``expected_until``.** That is precisely the region the missing
          truncation invented. Orphaned rows before the boundary come from other
          causes -- a deleted event, a re-timed occurrence -- and an occurrence that
          was legitimately billed stays billed.
        - **Only rows absent from the post-repair expansion.** Computed with
          ``MeteringService.expand_occurrence_identities``, the same function the
          meter and ``reconcile_period`` use, so a title-only split (where the
          continuation reuses the parent's start times) deletes nothing.
        """
        root_id = self._series_root_id(parent)
        rows = list(
            MeteredOccurrence.objects.filter(
                organization_id=parent.organization_id,
                event_id=root_id,
                occurrence_start__gt=item.expected_until,
            )
        )
        if not rows:
            return

        doomed: list[MeteredOccurrence] = []
        for row in rows:
            expected = self._expected_identities(row.subscription_id, row.billing_period_start)
            if expected is None:
                continue
            identity = OccurrenceIdentity(
                organization_id=row.organization_id,
                event_id=row.event_id,
                occurrence_start=row.occurrence_start,
            )
            if identity not in expected:
                doomed.append(row)

        if not doomed:
            return

        for row in doomed:
            self._write_backup({METERED_TABLE: _metered_row_to_dict(row)})
            if self._is_closed_period(row):
                self._metered_in_closed_periods += 1

        MeteredOccurrence.objects.filter(pk__in=[row.pk for row in doomed]).delete()
        self._metered_deleted += len(doomed)
        self.runtime.log(
            "INFO",
            f"{self.item_id(item)}: deleted {len(doomed)} phantom metered row(s) for root {root_id}",
        )

    @staticmethod
    def _series_root_id(parent: CalendarEvent) -> int:
        """Walk ``bulk_modification_parent`` to the series root.

        ``MeteredOccurrence.event_id`` holds the root, not the row that generated
        the occurrence, so a parent that is itself a continuation of an earlier
        split is billed under its ancestor's pk.
        """
        current = parent
        seen = {current.pk}
        for _depth in range(MAX_SERIES_CHAIN_DEPTH):
            parent_id = current.bulk_modification_parent_fk_id
            if parent_id is None or parent_id in seen:
                break
            ancestor = CalendarEvent.original_manager.filter(pk=parent_id).first()
            if ancestor is None:
                break
            current = ancestor
            seen.add(current.pk)
        return current.pk

    def _expected_identities(
        self, subscription_id: int, period_start: datetime.datetime
    ) -> set[OccurrenceIdentity] | None:
        """Identities the calendar expands to for one settlement period, cached.

        Returns ``None`` when the subscription is gone, which makes the caller leave
        the row alone -- deleting billing history we can no longer verify is not a
        trade this script is allowed to make.
        """
        key = (subscription_id, period_start)
        if key in self._expected_identity_cache:
            return self._expected_identity_cache[key]

        subscription = Subscription.objects.filter(pk=subscription_id).first()
        if subscription is None:
            return None

        # Late, and it has to be: `di_core.containers.container` is only assigned
        # in `DICoreConfig.ready()`, so a module-level `from ... import container`
        # binds the `None` the module starts with and never sees the wired
        # container.
        from di_core.containers import container

        if container is None:
            raise RuntimeError("DI container is not initialized; run inside the Django app context")
        metering_service = container.metering_service()
        window_start, window_end = resolve_settlement_period(subscription, period_start)
        identities = set(
            metering_service.expand_occurrence_identities(subscription, window_start, window_end)
        )
        self._expected_identity_cache[key] = identities
        return identities

    @staticmethod
    def _is_closed_period(row: MeteredOccurrence) -> bool:
        """Whether the row's billing period has already been rolled by cycle close.

        The rolled ``current_period_start`` is the durable close marker -- there is
        no period-close record model. Reported, not blocked: no overage has ever
        been charged (``_charge_overage`` short-circuits on the NULL limit every
        organization currently carries), so a closed period here means a settled
        usage ledger, not a settled invoice.
        """
        subscription = Subscription.objects.filter(pk=row.subscription_id).first()
        if subscription is None:
            return False
        return row.billing_period_start < subscription.current_period_start

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def apply_restore_row(self, table: str, row: dict[str, str]) -> None:
        if table == RULE_TABLE:
            RecurrenceRule.original_manager.filter(pk=int(row["id"])).update(
                count=int(row["count"]) if row["count"] else None,
                until=datetime.datetime.fromisoformat(row["until"]) if row["until"] else None,
            )
            return
        if table == METERED_TABLE:
            MeteredOccurrence.objects.update_or_create(
                pk=int(row["id"]),
                defaults={
                    "organization_id": int(row["organization_id"]),
                    "subscription_id": int(row["subscription_id"]),
                    "event_id": int(row["event_id"]),
                    "occurrence_start": datetime.datetime.fromisoformat(row["occurrence_start"]),
                    "billing_period_start": datetime.datetime.fromisoformat(
                        row["billing_period_start"]
                    ),
                    "is_within_allowance": row["is_within_allowance"] == "True",
                    "unit_price": row["unit_price"],
                },
            )
            return
        raise NotImplementedError(f"restore not implemented for table {table!r}")

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def run(self) -> int:
        exit_code = super().run()
        log = self.runtime.log
        log("INFO", "-" * 72)
        log("INFO", "classification summary (parents scanned but not repaired):")
        for reason in sorted(self.reported):
            log("INFO", f"  {reason}: {self.reported[reason]}")
        log("INFO", f"phantom metered rows deleted: {self._metered_deleted}")
        log(
            "INFO",
            f"  of which in already-closed billing periods: {self._metered_in_closed_periods}",
        )
        self.runtime.fsync_log()
        return exit_code


def _metered_row_to_dict(row: MeteredOccurrence) -> dict[str, Any]:
    return {
        "id": row.pk,
        "organization_id": row.organization_id,
        "subscription_id": row.subscription_id,
        "event_id": row.event_id,
        "occurrence_start": row.occurrence_start.isoformat(),
        "billing_period_start": row.billing_period_start.isoformat(),
        "is_within_allowance": str(row.is_within_allowance),
        "unit_price": str(row.unit_price),
    }


def build_config() -> ScriptConfig:
    return ScriptConfig(
        name=SCRIPT_NAME,
        log_dir=Path(".vinta-ai-workflows/one-off-runs"),
        batch_size=1000,
        csv_max_cells=1_000_000,
    )


# No ``__main__`` entry point on purpose. The runner is the management command at
# ``calendar_integration/management/commands/repair_untruncated_recurring_parents.py``,
# which resolves Django settings from the environment it runs in. A standalone
# ``python script.py`` path would have to guess a settings module, and guessing
# wrong on a production host is how a repair reads the wrong database.

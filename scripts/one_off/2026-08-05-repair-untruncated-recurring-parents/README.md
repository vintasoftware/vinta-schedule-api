# Repair untruncated recurring parents

**Author:** Hugo Bessa · **Authored:** 2026-08-05
**Related:** `ai-plans/TRACKING_BILLING_PLANS_AND_LIMITS.md` § "Phase 13 gating precondition" → "Resolved 2026-08-05 — truncation now persists"
**Runtime adapter:** `DjangoMgmtRuntime` ([scripts/one_off/_runtime_django.py](../_runtime_django.py))
**Runner:** [calendar_integration/management/commands/repair_untruncated_recurring_parents.py](../../../calendar_integration/management/commands/repair_untruncated_recurring_parents.py)

## What it does

Repairs recurring series whose parent was never truncated when a bulk modification split them.

Two upstream defects — both fixed on 2026-08-05 — left the parent's `RecurrenceRule` row without its `UNTIL`. `RecurrenceRuleSplitter` returned `copy.deepcopy` clones that preserve the saved model's `pk`, so both halves of a split aliased the original row and the continuation's `save()` overwrote the parent's truncation. Separately, on the `AvailableTime` / `BlockedTime` path the truncation was never written at all: `truncate_parent` assigned the same-pk clone to the one-to-one and saved the *parent*, and `OrganizationModel.save` only reassigns FK ids. An open-ended parent therefore never stopped and duplicated its series indefinitely.

The code fix stops new damage. It does not repair series already split. This script does that.

For each parent carrying a bulk-modification record, across `CalendarEvent`, `BlockedTime`, and `AvailableTime`, it recomputes the correct boundary — the last occurrence strictly before the **earliest** modification start — and writes it back as `UNTIL` with `COUNT` cleared. For `CalendarEvent` series it then deletes the `MeteredOccurrence` rows billed for phantom occurrences the repaired series no longer generates.

## What it deliberately does not repair

Only a parent whose rule is *provably* missing its truncation is touched. Every other classification is counted and logged, never written:

| Classification | Meaning | Action |
|---|---|---|
| `corrupt-unbounded` | `until IS NULL` — the defect's signature | **repaired** |
| `corrupt-count-set` | `until` correct but `count` still set | **repaired** |
| `already-correct` | split after the fix shipped | skipped |
| `no-recurrence-rule` | split at the first occurrence; that path always persisted correctly | skipped |
| `ambiguous-until-mismatch` | bounded at some *other* date — possibly a legitimate later edit | **skipped, logged WARN** |
| `no-occurrence-before-split` | rule generates nothing before the split | **skipped, logged WARN** |

The two WARN rows need a human. Grep the run log for `needs review` and review each by id before deciding whether to force anything.

## Metering cleanup — scope and residual risk

Deletion is confined by two guards:

- **Only rows past the repaired `UNTIL`.** That is exactly the region the missing truncation invented. Orphans before the boundary come from other causes — a deleted event, a re-timed occurrence — and an occurrence that was legitimately billed stays billed.
- **Only rows absent from the post-repair expansion,** computed with `MeteringService.expand_occurrence_identities`, the same function the meter and `reconcile_period` use. A title-only split, where the continuation reuses the parent's start times, therefore deletes nothing.

**No money is at stake.** Every organization is on `unlimited` (NULL `event_occurrences`), and `CycleCloseService._charge_overage` short-circuits on a NULL limit, so no overage has ever been charged. Deleting these rows rewrites the usage ledger, not an invoice. Rows in already-closed billing periods (`billing_period_start < subscription.current_period_start`) are still deleted, but counted separately in the summary so the operator can see how much settled history moved.

This is a deliberate departure from `reconcile_period`'s "read-only by design" stance and `MeteredOccurrence`'s "billed at most once ever" invariant. It was requested explicitly. If that trade stops being acceptable, drop `_delete_phantom_metered` — the calendar repair stands on its own.

## Run

The management command is the only entry point. `script.py` has no `__main__` block on purpose: a standalone path would have to guess a settings module, and guessing wrong on a production host is how a repair reads the wrong database. The command resolves settings from the environment it runs in.

```bash
# Dry-run first — always. Prints every planned repair and the classification summary.
uv run python manage.py repair_untruncated_recurring_parents

# Apply
uv run python manage.py repair_untruncated_recurring_parents --apply

# Typical background launch
nohup python manage.py repair_untruncated_recurring_parents --apply > /dev/null 2>&1 &
```

Locally, prefix with `docker compose run --rm api`. **Never run `--apply` against production settings from a laptop** — run it inside the production execution environment.

## Monitor

```bash
uv run python manage.py repair_untruncated_recurring_parents --status
tail -f .vinta-ai-workflows/one-off-runs/2026-08-05-repair-untruncated-recurring-parents/run.log
```

`--status` is read-only and never takes the lease of the run it reports on.

## Interrupt

```bash
kill -TERM "$(cat .vinta-ai-workflows/one-off-runs/2026-08-05-repair-untruncated-recurring-parents/lease.pid)"
```

The current item finishes, CSV backups and the log flush, artifacts upload, the lease releases, exit 0. A second signal force-exits.

## Resume

```bash
uv run python manage.py repair_untruncated_recurring_parents --apply --resume
```

Idempotency is state-based as well: a re-run without `--resume` reclassifies every repaired parent as `already-correct` and writes nothing.

## Restore

```bash
uv run python manage.py repair_untruncated_recurring_parents \
  --restore .vinta-ai-workflows/one-off-runs/2026-08-05-repair-untruncated-recurring-parents
```

Restores `calendar_integration_recurrencerule.NNN.csv` (puts `count` / `until` back) and `payments_meteredoccurrence.NNN.csv` (re-inserts deleted rows with their original pks).

If the local run dir is gone, pull the run's artifacts down from storage first (see below) and point `--restore` at the directory you download them into.

## Where the artifacts go

`run.log`, `processed.txt`, and every CSV backup chunk are written to `.vinta-ai-workflows/one-off-runs/<script>/` (gitignored) and then copied into **Django's default storage** — `MediaStorage` over S3 in production, Floci locally — under:

```
one-off-runs/2026-08-05-repair-untruncated-recurring-parents/<YYYYMMDDTHHMMSSffffffZ>/
```

No `ONE_OFF_S3_*` env vars are involved; the runtime uses whatever `STORAGES["default"]` is configured for the environment. The remote key is per-run, so a second run cannot overwrite the first run's evidence — worth knowing because the *local* run dir does reuse filenames (the CSV chunk writer opens with `"w"`).

Upload is best-effort by contract: if the bucket is unreachable the failure is logged and the run still completes, because the on-disk copy is authoritative.

## Expected runtime and timing

Proportional to the number of bulk-modification records, not to calendar size — one grouped scan per object type plus one `UPDATE` per corrupt parent. Event parents additionally cost one `expand_occurrence_identities` per distinct `(subscription, billing period)`, cached across items, which is the dominant term. Expect minutes, not hours, at current data volumes.

**Run off-peak.** The metering expansion reads the calendar tables broadly, and `process` takes a `SELECT ... FOR UPDATE` on each parent row.

## Done looks like

The classification summary reports zero `corrupt-unbounded` and zero `corrupt-count-set`, and this returns no rows:

```sql
SELECT bm.parent_event_fk_id, r.count, r.until
FROM calendar_integration_eventbulkmodification bm
JOIN calendar_integration_calendarevent e ON e.id = bm.parent_event_fk_id
JOIN calendar_integration_recurrencerule r ON r.id = e.recurrence_rule_fk_id
WHERE r.until IS NULL;
```

Repeat for `blockedtimebulkmodification` / `calendar_integration_blockedtime` and `availabletimebulkmodification` / `calendar_integration_availabletime`.

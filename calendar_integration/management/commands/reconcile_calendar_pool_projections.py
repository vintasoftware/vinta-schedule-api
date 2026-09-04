"""Recompute the calendar-pool roster projection and report (or repair) drift.

A slot's bookable roster is the union of its inline ``CalendarGroupSlotMembership``
rows (``source_pool IS NULL``) and rows *projected* from the ``CalendarPool``s
attached to it (``source_pool`` set). The projection is written, not computed on
read -- which buys correctness for the nine call sites that reach the roster
through ``memberships``, at the cost of the projection being able to drift from
the pools it is derived from. This command is the drift mitigation the Calendar
Pools plan promises: it recomputes the projected half from scratch, compares it
to what is stored, and reports every difference.

It never reads and never writes inline rows. Every query it issues is filtered to
``source_pool IS NOT NULL``, so a slot with no pools attached is invisible to it
and a hand-curated inline roster cannot be touched by a repair.

Reporting-without-writing is the default: running this with no flags at all
reports and changes nothing. Pass ``--fix`` to apply. ``--dry-run`` is also
accepted explicitly, but only to be refused as a conflict when combined with
``--fix`` -- passing both raises ``CommandError`` rather than silently taking
``--fix``'s write, since a flag whose entire purpose is "this is safe" must not
be overridable by accident. Per the plan's Risk & Rollout Notes, a reported
difference should be treated as a bug in the reconcile path in
``CalendarGroupService._reconcile_slot_pools`` and investigated, not merely
repaired.
"""

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from calendar_integration.models import (
    CalendarGroupSlotMembership,
    CalendarGroupSlotPool,
    CalendarPoolMembership,
)
from common.organization_context import organization_context
from organizations.models import Organization


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Recompute the slot <-> pool roster projection and report differences."""

    help = (
        "Recompute CalendarGroupSlotMembership rows projected from attached "
        "CalendarPools and report differences. Dry-run unless --fix is passed."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization-id",
            type=int,
            default=None,
            help="Limit the sweep to one organization. Default: every organization.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help=(
                "Report differences without writing. This is already the default "
                "behavior with no flags at all -- passing this explicitly only "
                "matters together with --fix, which is then refused as a conflict "
                "instead of silently taking --fix's write."
            ),
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            default=False,
            help="Apply the recomputed projection.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        apply_fix: bool = options["fix"]
        dry_run: bool = options["dry_run"]
        if dry_run and apply_fix:
            raise CommandError(
                "--dry-run and --fix are mutually exclusive: pass --fix alone to "
                "write, or neither flag (the default) to only report."
            )
        organization_id: int | None = options["organization_id"]

        organizations = Organization.objects.all().order_by("id")
        if organization_id is not None:
            organizations = organizations.filter(id=organization_id)
            if not organizations.exists():
                raise CommandError(f"Organization {organization_id} does not exist.")

        total_missing = 0
        total_orphaned = 0
        for organization in organizations:
            # Management commands bind tenancy from their own arguments, never
            # from request state -- one organization at a time so every read
            # below goes through the organization-scoped manager.
            with organization_context(organization):
                missing, orphaned = self._reconcile_organization(organization, apply_fix=apply_fix)
            total_missing += len(missing)
            total_orphaned += len(orphaned)

        verb = "repaired" if apply_fix else "would repair"
        if total_missing == 0 and total_orphaned == 0:
            self.stdout.write(self.style.SUCCESS("Projection is consistent; no drift found."))
            return

        self.stdout.write(
            self.style.WARNING(
                f"DRIFT DETECTED: {total_missing} missing projected row(s), "
                f"{total_orphaned} orphaned projected row(s) — {verb}."
            )
        )
        if not apply_fix:
            self.stdout.write(
                self.style.NOTICE("Dry run: nothing was written. Re-run with --fix to apply.")
            )
        self.stdout.write(
            self.style.NOTICE(
                "Treat any difference as a bug in "
                "CalendarGroupService._reconcile_slot_pools rather than a routine repair."
            )
        )

    def _reconcile_organization(
        self, organization: Organization, *, apply_fix: bool
    ) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """Compare stored and recomputed projections for one organization.

        Returns ``(missing, orphaned)`` as lists of ``(slot_id, pool_id,
        calendar_id)`` triples: rows the attachments imply but that are absent,
        and stored projected rows no attachment justifies.
        """
        org_id = organization.id

        attachments = list(
            CalendarGroupSlotPool.objects.filter_by_organization(org_id).values_list(
                "slot_fk_id", "pool_fk_id"
            )
        )
        pool_ids = {pool_id for _, pool_id in attachments}
        rosters: dict[int, set[int]] = {pool_id: set() for pool_id in pool_ids}
        if pool_ids:
            for pool_id, calendar_id in (
                CalendarPoolMembership.objects.filter_by_organization(org_id)
                .filter(pool_fk_id__in=pool_ids)
                .values_list("pool_fk_id", "calendar_fk_id")
            ):
                rosters[pool_id].add(calendar_id)

        expected = {
            (slot_id, pool_id, calendar_id)
            for slot_id, pool_id in attachments
            for calendar_id in rosters[pool_id]
        }
        # ``projected()`` is what keeps inline rows out of this entirely: they
        # are not derived from anything, so they can be neither missing nor
        # orphaned, and a repair must never consider deleting one.
        stored = set(
            CalendarGroupSlotMembership.objects.filter_by_organization(org_id)
            .projected()
            .values_list("slot_fk_id", "source_pool_fk_id", "calendar_fk_id")
        )

        missing = sorted(expected - stored)
        orphaned = sorted(stored - expected)

        for slot_id, pool_id, calendar_id in missing:
            self.stdout.write(
                f"  org={org_id} MISSING slot={slot_id} pool={pool_id} calendar={calendar_id}"
            )
        for slot_id, pool_id, calendar_id in orphaned:
            self.stdout.write(
                f"  org={org_id} ORPHANED slot={slot_id} pool={pool_id} calendar={calendar_id}"
            )

        if apply_fix and (missing or orphaned):
            # Writes CalendarGroupSlotMembership directly -- the projection
            # itself -- never CalendarPoolMembership (the pool roster). The
            # post_save/post_delete receivers in calendar_integration.signals
            # that reproject on a CalendarPoolMembership change are therefore
            # not in this call chain and this repair cannot recurse into them.
            with transaction.atomic():
                for slot_id, pool_id, calendar_id in orphaned:
                    CalendarGroupSlotMembership.objects.filter_by_organization(
                        org_id
                    ).projected().filter(
                        slot_fk_id=slot_id,
                        source_pool_fk_id=pool_id,
                        calendar_fk_id=calendar_id,
                    ).delete()
                CalendarGroupSlotMembership.objects.bulk_create(
                    [
                        CalendarGroupSlotMembership(
                            organization=organization,
                            slot_fk_id=slot_id,
                            calendar_fk_id=calendar_id,
                            source_pool_fk_id=pool_id,
                        )
                        for slot_id, pool_id, calendar_id in missing
                    ]
                )
            # A production repair inserts/deletes rows with no CalendarGroupService
            # audit trail (that trail is for user-driven writes; this is an
            # operator-run sweep) -- logged at INFO, in addition to stdout, so the
            # repair is traceable after the fact from wherever logs are shipped.
            logger.info(
                "reconcile_calendar_pool_projections repaired org=%s: deleted=%s inserted=%s",
                org_id,
                orphaned,
                missing,
            )

        return missing, orphaned

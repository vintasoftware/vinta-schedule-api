"""Re-run reconciliation for a named closed billing period, for finance.

Read-only: recomputes what a subscription's cycle *should* have metered from the
calendar and compares it against the ``MeteredOccurrence`` rows actually recorded,
reporting drift both ways. It never writes and never charges — it is the
after-the-fact audit half of cycle close, run on demand rather than on the beat
schedule (``payments.tasks.close_billing_periods``).

The reconciliation it runs is the same ``MeteringService.reconcile_period`` cycle
close runs; this command just lets finance point it at any past period by
subscription id and a moment inside the period.
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from payments.models import MeteredOccurrence, Subscription
from payments.services.subscription_service import resolve_billing_period


class Command(BaseCommand):
    """Re-run reconciliation for one subscription's closed billing period."""

    help = "Re-run metering reconciliation for a named closed billing period (read-only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--subscription-id",
            type=int,
            required=True,
            help="Subscription whose closed period to reconcile.",
        )
        parser.add_argument(
            "--period",
            type=str,
            required=True,
            help=(
                "Any moment inside the billing period to reconcile, as an ISO 8601 "
                "datetime (e.g. 2025-06-15 or 2025-06-15T12:00:00+00:00). The exact "
                "cycle bounds are resolved from the subscription via "
                "resolve_billing_period, the same function the meter stamped the "
                "period with."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        subscription_id = options["subscription_id"]
        period_raw = options["period"]

        subscription = Subscription.objects.filter(pk=subscription_id).first()
        if subscription is None:
            raise CommandError(f"Subscription {subscription_id} does not exist.")

        try:
            moment = datetime.datetime.fromisoformat(period_raw)
        except ValueError as exc:
            raise CommandError(f"Could not parse --period {period_raw!r} as ISO 8601.") from exc
        if timezone.is_naive(moment):
            moment = moment.replace(tzinfo=datetime.UTC)

        # Deferred import: `di_core.containers.container` is only assigned in
        # `DICoreConfig.ready()`, so a module-level import binds `None`.
        from di_core.containers import container

        if container is None:
            raise CommandError("DI container is not wired.")
        metering_service = container.metering_service()

        period_start, period_end = resolve_billing_period(subscription, moment)
        report = metering_service.reconcile_period(subscription, moment)
        overage_total = MeteredOccurrence.objects.for_billing_period(
            subscription.pk, period_start
        ).overage_total()

        self.stdout.write(
            f"Subscription {subscription.pk} — period "
            f"[{period_start.isoformat()}, {period_end.isoformat()})"
        )
        self.stdout.write(f"  expected occurrences (recomputed): {report.expected_count}")
        self.stdout.write(f"  metered occurrences (recorded):    {report.metered_count}")
        self.stdout.write(f"  overage owed (sum of stamped unit prices): {overage_total}")
        self.stdout.write(
            f"  drift: {report.drift} "
            f"(unmetered={len(report.unmetered)}, orphaned={len(report.orphaned)})"
        )

        if report.is_clean:
            self.stdout.write(self.style.SUCCESS("  reconciled clean (identity drift == 0)."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"  DRIFT DETECTED ({report.drift}). Escalate — reconciliation reports, it "
                    "does not repair. `unmetered` rows are usage never billed (re-run the sweep); "
                    "`orphaned` rows may be a legitimately deleted event, since a billed "
                    "occurrence stays billed."
                )
            )

        # The blind spot is stated on every run, clean or not, so no one reads a
        # clean report as proof the invoice is correct.
        self.stdout.write(
            self.style.NOTICE(
                "  NOTE: reconciliation audits occurrence *identity* only, not pricing "
                "(is_within_allowance / unit_price). It is also structurally blind to the "
                "modify-then-sweep-once over-count (a bulk modification that does not truncate "
                "the parent series inflates both the meter and this recomputation identically), "
                "so a clean report means 'the metered set matches the calendar's current "
                "expansion', not 'this invoice is correct'."
            )
        )

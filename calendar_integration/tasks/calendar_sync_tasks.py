import datetime
import logging
from typing import Annotated, Literal

from django.utils import timezone

from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarSyncTriggerSource,
)
from calendar_integration.models import (
    Calendar,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from payments.exceptions import OverLimitError
from payments.services.entitlement_service import EntitlementService
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


def _restricted_or_skip(
    entitlement_service: EntitlementService, organization: Organization
) -> bool:
    """``True`` when ``organization``'s billing root is ``RESTRICTED`` (Phase 11) --
    logs and lets the caller early-return rather than run the sync.

    Consults ``EntitlementService.is_billing_root_restricted`` -- the **same**
    predicate the write guard (``EntitlementService.check_limit`` /
    ``check_postpaid_allowance`` / ``check_not_restricted``) and the
    ``request_*`` enqueue guards (``CalendarSyncService._check_not_restricted``)
    all consult. One definition of "restricted", not a second one re-derived at
    the task layer: if this checked something else (a local flag, a different
    query), an organization could be write-blocked while its sync kept running
    (continued third-party spend) or sync-paused while still writable -- the
    plan's own named recurring failure shape.

    This is defense in depth, not the primary gate: the ``request_*`` methods
    already refuse to enqueue a restricted organization's sync work in the
    first place (``CalendarSyncService._check_not_restricted``, raised as
    ``OverLimitError`` before anything is queued). This early-return exists for
    work that was already queued *before* the organization became restricted
    (mid-flight at the moment ``GRACE`` expired) -- it must not run once it is
    picked up, mirroring the existing early return for a missing organization
    just above each call site.
    """
    if not entitlement_service.is_billing_root_restricted(organization):
        return False
    logger.info(
        "Skipping calendar sync for organization %s: organization is RESTRICTED.",
        organization.pk,
    )
    return True


def _authenticate_or_skip(calendar_service, account, organization) -> bool:
    """Authenticate the service, treating a missing provider entitlement at
    authenticate-time as a **skip**.

    Only wraps ``calendar_service.authenticate(...)``: the *account's* provider
    entitlement gate. It does not, and cannot, catch the separate, calendar-scoped
    gate on ``_get_write_adapter_for_calendar`` -- no caller in this module reaches
    that method today, so the distinction is not yet observable, but a future task
    that did would need its own guard around that call, not this one.

    Every task in this module is scheduled, not user-triggered. An organization whose
    plan omits `external_calendar_google` / `external_calendar_microsoft` would otherwise
    turn each scheduled run into a hard task failure: these tasks declare no
    `autoretry_for`, and `CELERY_TASK_ACKS_LATE=True` means a raising task is redelivered
    and fails again, so a billing state that is *working as intended* would manufacture a
    permanent stream of alarming failures.

    "Not entitled to sync" is a legitimate terminal state for a sync run, so it is logged
    and the task returns successfully. The org still cannot sync -- the guard did its job;
    it just does not pretend the scheduler is broken. Interactive callers (REST/GraphQL)
    keep the 402, since a user asking to connect a calendar should be told why it failed.
    """
    try:
        calendar_service.authenticate(account=account, organization=organization)
    except OverLimitError as exc:
        logger.info(
            "Skipping calendar sync for organization %s: %s",
            organization.pk,
            exc.as_error_body()["detail"],
        )
        return False
    return True


@app.task
@inject
def import_account_calendars_task(
    account_type: Literal["social_account", "google_service_account"],
    account_id: int,
    organization_id: int,
    calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
    sync_after_import: bool = True,
):
    """
    Celery task to import calendars for a given account.
    This task will call the CalendarService to perform the import operation.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    if _restricted_or_skip(entitlement_service, organization):
        return

    if account_type == "social_account":
        social_account = SocialAccount.objects.filter(id=account_id).first()
        if social_account:
            account = social_account.user
        else:
            account = None
    else:
        account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(
                organization_id=organization_id
            )
            .filter(id=account_id)
            .first()
        )

    if not account:
        return
    if not _authenticate_or_skip(calendar_service, account, organization):
        return
    calendar_service.import_account_calendars(sync_after_import=sync_after_import)


@app.task
@inject
def sync_calendar_task(
    account_type: Literal["social_account", "google_service_account"],
    account_id: int,
    calendar_sync_id: int,
    organization_id: int,
    calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
):
    """
    Celery task to sync a calendar by its ID.
    This task will call the CalendarService to perform the sync operation.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    if _restricted_or_skip(entitlement_service, organization):
        return

    calendar_sync = CalendarSync.objects.filter_by_organization(
        organization_id=organization_id
    ).get_not_started_calendar_sync(calendar_sync_id)
    if account_type == "social_account":
        social_account = SocialAccount.objects.filter(id=account_id).first()
        if social_account:
            account = social_account.user
        else:
            account = None
    else:
        account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(
                organization_id=organization_id
            )
            .filter(id=account_id)
            .first()
        )

    if not account or not calendar_sync:
        return

    if not _authenticate_or_skip(calendar_service, account, organization):
        return
    calendar_service.sync_events(calendar_sync)


@app.task
@inject
def import_organization_calendar_resources_task(
    account_type: Literal["social_account", "google_service_account"],
    account_id: int,
    organization_id: int,
    import_workflow_state_id: int,
    calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
):
    """
    Celery task to import organization calendar resources.
    This task will call the CalendarService to perform the import operation.
    """

    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    if _restricted_or_skip(entitlement_service, organization):
        return

    import_workflow_state = (
        CalendarOrganizationResourcesImport.objects.filter_by_organization(
            organization_id=organization_id
        )
        .filter(
            id=import_workflow_state_id, status=CalendarOrganizationResourceImportStatus.NOT_STARTED
        )
        .first()
    )

    if not import_workflow_state:
        return

    if account_type == "social_account":
        social_account = SocialAccount.objects.filter(id=account_id).first()
        if social_account:
            account = social_account.user
        else:
            account = None
    else:
        account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(
                organization_id=organization_id
            )
            .filter(id=account_id)
            .first()
        )

    if not account:
        return

    if not _authenticate_or_skip(calendar_service, account, organization):
        return
    calendar_service.import_organization_calendar_resources(import_workflow_state)


#: How far back a recovery resync (``resync_organization_calendars_task``) looks.
#: The exact moment an organization became RESTRICTED is not stamped anywhere
#: (``Subscription`` has no ``restricted_at`` column), so this is a bounded
#: lookback wide enough to cover a typical grace-plus-restricted episode rather
#: than an attempt to reconstruct the exact window. Paired with a forward window
#: matching ``OrganizationService.request_rooms_sync``'s existing default so a
#: recovery resync is not narrower than an ordinary manual one.
RESYNC_AFTER_RECOVERY_LOOKBACK = datetime.timedelta(days=30)
RESYNC_AFTER_RECOVERY_LOOKAHEAD = datetime.timedelta(days=365)


@app.task
@inject
def resync_organization_calendars_task(
    organization_id: int,
    calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]],
) -> None:
    """Reconcile ``organization``'s calendars after its billing root recovers out
    of ``RESTRICTED`` (Phase 11).

    Sync is paused for the whole time an organization's billing root is
    ``RESTRICTED`` (see ``CalendarSyncService._check_not_restricted`` and this
    module's own ``_restricted_or_skip``), so the calendar can have drifted from
    both directions while paused: events created locally were never pushed to
    the provider (writes were also blocked, so this side is actually inert --
    see ``EntitlementService.check_not_restricted`` -- but provider-side changes
    made during the pause were never pulled in). This task re-syncs every
    actively-owned calendar over a window that covers the pause, so recovery is
    a real reconciliation, not just "sync resumes from here forward".

    Fanned out **per pooled organization** by the caller
    (``DunningService``), not per calendar directly, mirroring
    ``OrganizationService.request_all_calendars_sync``'s per-calendar resolution
    of the syncing account: only calendars with a resolvable owner (a
    ``CalendarOwnership`` row with a member) and that member's linked
    ``SocialAccount`` for the calendar's provider can be resynced here --
    ``GoogleCalendarServiceAccount``-owned (service-account) calendars are not
    covered, the same limitation ``request_all_calendars_sync`` already has.
    Actual per-calendar sync work is queued via ``CalendarSyncService
    .request_calendar_sync`` (through ``CalendarService.request_calendar_sync``),
    which re-checks ``is_billing_root_restricted`` itself -- if the organization
    somehow re-entered RESTRICTED between this task starting and each per-calendar
    call, that later call refuses to queue, rather than this task racing ahead on
    a stale "we just recovered" assumption.

    A missing organization or a still-restricted one (a redelivery racing a
    further billing-state change) is a no-op, mirroring every other task in this
    module.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    if _restricted_or_skip(entitlement_service, organization):
        return

    now = timezone.now()
    start_datetime = now - RESYNC_AFTER_RECOVERY_LOOKBACK
    end_datetime = now + RESYNC_AFTER_RECOVERY_LOOKAHEAD

    calendars = Calendar.objects.filter_by_organization(organization_id).exclude_inactive()
    for calendar in calendars:
        ownership = (
            CalendarOwnership.objects.filter_by_organization(organization_id)
            .filter(calendar=calendar, membership_user_id__isnull=False)
            .order_by("-is_default", "id")
            .first()
        )
        if ownership is None:
            continue

        social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=calendar.provider
        ).first()
        if social_account is None:
            continue

        if not _authenticate_or_skip(calendar_service, social_account.user, organization):
            continue

        calendar_service.request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=True,
            trigger_source=CalendarSyncTriggerSource.ADMIN,
        )

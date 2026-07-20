import logging
from typing import Annotated, Literal

from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject

from calendar_integration.constants import CalendarOrganizationResourceImportStatus
from calendar_integration.models import (
    CalendarOrganizationResourcesImport,
    CalendarSync,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from payments.exceptions import OverLimitError
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


def _authenticate_or_skip(calendar_service, account, organization) -> bool:
    """Authenticate the service, treating a missing provider entitlement as a **skip**.

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
    sync_after_import: bool = True,
):
    """
    Celery task to import calendars for a given account.
    This task will call the CalendarService to perform the import operation.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
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
):
    """
    Celery task to sync a calendar by its ID.
    This task will call the CalendarService to perform the sync operation.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
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
):
    """
    Celery task to import organization calendar resources.
    This task will call the CalendarService to perform the import operation.
    """

    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
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

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
from vinta_schedule_api.celery import app


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
        account = SocialAccount.objects.filter(id=account_id).first()
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

    calendar_service.authenticate(account=account, organization=organization)
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
        account = SocialAccount.objects.filter(id=account_id).first()
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

    calendar_service.authenticate(account=account, organization=organization)
    calendar_service.import_organization_calendar_resources(import_workflow_state)

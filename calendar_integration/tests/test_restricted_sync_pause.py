"""Phase 11: sync pause and resync-on-recovery.

Spec use-case 5 (restricted half): a restricted organization stops costing us
third-party spend -- background calendar sync pauses -- and, on recovering
out of RESTRICTED, resumes with a real reconciliation, not merely "sync
resumes from here forward".

Both the ``request_*`` enqueue guards (``CalendarSyncService
._check_not_restricted``) and the task-body early returns
(``calendar_integration.tasks.calendar_sync_tasks._restricted_or_skip``)
consult the same predicate the write guard uses --
``EntitlementService.is_billing_root_restricted`` -- proven here by driving
the real methods/tasks rather than asserting against the predicate in
isolation.
"""

import datetime
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarSyncStatus
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarOwnership,
    CalendarSync,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.calendar_webhook_service import CalendarWebhookService
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from calendar_integration.tasks.calendar_sync_tasks import (
    import_account_calendars_task,
    import_organization_calendar_resources_task,
    resync_organization_calendars_task,
    sync_calendar_task,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, Entitlement
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement
from payments.services.dunning_service import DunningService
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService
from users.models import Profile, User


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription

# The Google adapter refuses to construct without these -- see
# ``test_sync_limit_enforcement.py``'s identical decorator.
_with_google_credentials = override_settings(
    GOOGLE_CLIENT_ID="test-google-client-id",
    GOOGLE_CLIENT_SECRET="test-google-client-secret",  # noqa: S106 - dummy value, not a credential
)


def _organization_with_billing_state(
    billing_state: str, *, grant_google_entitlement: bool = False
) -> Organization:
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=billing_state,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    if grant_google_entitlement:
        # `authenticate()`'s provider-entitlement gate (Phase 6c) fails closed on
        # an absent row -- irrelevant to what this module tests, but needed for
        # a real `CalendarService.authenticate()` call to succeed rather than
        # being silently skipped by `_authenticate_or_skip`.
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=Entitlement.EXTERNAL_CALENDAR_GOOGLE,
            is_enabled=True,
        )
    return organization


def _google_account(organization: Organization, email: str) -> SocialAccount:
    user = User.objects.create_user(email=email, password="pw")  # noqa: S106
    Profile.objects.create(user=user)
    account = SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid=f"uid-{email}"
    )
    SocialToken.objects.create(
        account=account,
        token="access",  # noqa: S106
        token_secret="refresh",  # noqa: S106
        expires_at=timezone.now() + datetime.timedelta(hours=1),
    )
    return account


# ---------------------------------------------------------------------------
# request_* methods refuse to enqueue for a RESTRICTED organization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequestMethodsDoNotEnqueueWhileRestricted:
    def test_request_calendar_sync_raises_and_creates_no_row(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="restricted-sync-cal",
        )
        context = CalendarServiceContext(
            organization=organization,
            user_or_token=None,
            # Non-None so `is_authenticated_calendar_service` passes and the
            # method reaches the restricted-state guard being tested here,
            # rather than failing the auth precondition first.
            account=MagicMock(),
            calendar_adapter=MagicMock(),
            calendar_permission_service=None,
            calendar_side_effects_service=None,
            entitlement_service=EntitlementService(),
        )
        service = CalendarSyncService(context=context, calendar_cache={}, host=MagicMock())

        with patch(
            "calendar_integration.tasks.calendar_sync_tasks.sync_calendar_task.delay"
        ) as dispatched:
            with pytest.raises(OverLimitError) as exc_info:
                service.request_calendar_sync(
                    calendar=calendar,
                    start_datetime=timezone.now(),
                    end_datetime=timezone.now() + datetime.timedelta(days=1),
                )

        assert exc_info.value.remedy == "resolve_billing"
        dispatched.assert_not_called()
        assert not CalendarSync.objects.filter(calendar=calendar).exists()

    def test_request_calendars_import_raises_and_does_not_enqueue(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        context = CalendarServiceContext(
            organization=organization,
            user_or_token=None,
            account=GoogleCalendarServiceAccount.objects.create(
                email="restricted-service-account@example.com",
                admin_email="admin@example.com",
                private_key_id="test-key-id",
                private_key="test-private-key",
                organization=organization,
            ),
            calendar_adapter=MagicMock(),
            calendar_permission_service=None,
            calendar_side_effects_service=None,
            entitlement_service=EntitlementService(),
        )
        service = CalendarSyncService(context=context, calendar_cache={}, host=MagicMock())

        with patch.object(import_account_calendars_task, "delay") as dispatched:
            with pytest.raises(OverLimitError):
                service.request_calendars_import()

        dispatched.assert_not_called()

    def test_request_organization_calendar_resources_import_raises_and_does_not_enqueue(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        context = CalendarServiceContext(
            organization=organization,
            user_or_token=None,
            account=GoogleCalendarServiceAccount.objects.create(
                email="restricted-org-import-account@example.com",
                admin_email="admin@example.com",
                private_key_id="test-key-id",
                private_key="test-private-key",
                organization=organization,
            ),
            calendar_adapter=MagicMock(),
            calendar_permission_service=None,
            calendar_side_effects_service=None,
            entitlement_service=EntitlementService(),
        )
        service = CalendarSyncService(context=context, calendar_cache={}, host=MagicMock())

        with patch.object(import_organization_calendar_resources_task, "delay") as dispatched:
            with pytest.raises(OverLimitError):
                service.request_organization_calendar_resources_import(
                    start_time=timezone.now(),
                    end_time=timezone.now() + datetime.timedelta(days=1),
                )

        dispatched.assert_not_called()

    def test_webhook_triggered_sync_is_skipped_not_raised(self):
        """The webhook-triggered path has no user to show a 402 to (a
        server-to-server provider push) -- it degrades to a logged skip rather
        than raising, mirroring the existing missing-provider-entitlement
        degrade in ``process_webhook_notification``."""
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="restricted-webhook-cal",
        )
        webhook_event = baker.make(
            "calendar_integration.CalendarWebhookEvent",
            organization=organization,
            provider=CalendarProvider.GOOGLE,
        )
        context = CalendarServiceContext(
            organization=organization,
            user_or_token=None,
            account=None,
            calendar_adapter=MagicMock(),
            calendar_permission_service=None,
            calendar_side_effects_service=None,
            entitlement_service=EntitlementService(),
        )
        host = MagicMock()
        service = CalendarWebhookService(context=context, calendar_cache={}, host=host)

        result = service.request_webhook_triggered_sync(
            external_calendar_id=calendar.external_id, webhook_event=webhook_event
        )

        assert result is None
        host.request_calendar_sync.assert_not_called()


# ---------------------------------------------------------------------------
# A directly-invoked task early-returns for a RESTRICTED organization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTaskBodiesEarlyReturnWhileRestricted:
    def test_sync_calendar_task_returns_instead_of_syncing(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        account = _google_account(organization, "restricted-sync-task@example.com")
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="restricted-task-cal",
        )
        calendar_sync = baker.make(
            CalendarSync,
            organization=organization,
            calendar=calendar,
            status=CalendarSyncStatus.NOT_STARTED,
            start_datetime=timezone.now(),
            end_datetime=timezone.now() + datetime.timedelta(days=1),
        )

        with patch.object(CalendarService, "sync_events") as synced:
            sync_calendar_task(
                account_type="social_account",
                account_id=account.id,
                calendar_sync_id=calendar_sync.id,
                organization_id=organization.id,
            )

        synced.assert_not_called()

    def test_import_account_calendars_task_returns_instead_of_importing(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        account = _google_account(organization, "restricted-import-task@example.com")

        with patch.object(CalendarService, "import_account_calendars") as imported:
            import_account_calendars_task(
                account_type="social_account",
                account_id=account.id,
                organization_id=organization.id,
            )

        imported.assert_not_called()

    def test_import_organization_calendar_resources_task_returns_instead_of_importing(self):
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        account = _google_account(organization, "restricted-org-import-task@example.com")
        import_state = baker.make(
            "calendar_integration.CalendarOrganizationResourcesImport",
            organization=organization,
            start_time=timezone.now(),
            end_time=timezone.now() + datetime.timedelta(days=1),
        )

        with patch.object(CalendarService, "import_organization_calendar_resources") as imported:
            import_organization_calendar_resources_task(
                account_type="social_account",
                account_id=account.id,
                organization_id=organization.id,
                import_workflow_state_id=import_state.id,
            )

        imported.assert_not_called()


# ---------------------------------------------------------------------------
# Resync on recovery: dispatch
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecoveryDispatchesAResync:
    def _active_org_with_owned_calendar(self) -> tuple[Organization, Calendar]:
        organization = _organization_with_billing_state(BillingState.RESTRICTED)
        account = _google_account(organization, "recovery-dispatch@example.com")
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="recovery-cal",
        )
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=account.user,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        baker.make(
            CalendarOwnership,
            organization=organization,
            calendar=calendar,
            membership_user_id=account.user_id,
            is_default=True,
        )
        return organization, calendar

    def test_resolve_payment_success_from_restricted_queues_the_resync(self):
        organization, _calendar = self._active_org_with_owned_calendar()
        subscription = Subscription.objects.get(organization=organization)
        entitlement_service = EntitlementService()
        dunning_service = DunningService(
            subscription_service=SubscriptionService(),
            entitlement_service=entitlement_service,
        )

        with (
            patch(
                "payments.services.dunning_service.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
            patch.object(resync_organization_calendars_task, "delay") as dispatched,
        ):
            dunning_service.resolve_payment_success(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        dispatched.assert_called_once_with(organization_id=organization.pk)

    def test_resolve_payment_success_from_grace_does_not_queue_a_resync(self):
        """GRACE never paused sync in the first place -- leaving GRACE has
        nothing to reconcile."""
        organization = _organization_with_billing_state(BillingState.GRACE)
        subscription = Subscription.objects.get(organization=organization)
        dunning_service = DunningService(
            subscription_service=SubscriptionService(),
            entitlement_service=EntitlementService(),
        )

        with (
            patch(
                "payments.services.dunning_service.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
            patch.object(resync_organization_calendars_task, "delay") as dispatched,
        ):
            dunning_service.resolve_payment_success(subscription)

        dispatched.assert_not_called()


# ---------------------------------------------------------------------------
# Resync on recovery: it actually enqueues per-calendar sync work
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestResyncTaskQueuesPerCalendarSync:
    @_with_google_credentials
    def test_resync_task_creates_a_calendar_sync_for_the_owned_calendar(self):
        """Once the organization is no longer restricted,
        ``resync_organization_calendars_task`` resolves the calendar's owner
        and drives a real ``request_calendar_sync`` call for it -- a
        ``CalendarSync`` row is the observable proof that a resync was
        actually requested, not merely that the task ran."""
        organization = _organization_with_billing_state(
            BillingState.ACTIVE, grant_google_entitlement=True
        )
        account = _google_account(organization, "resync-task@example.com")
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="resync-task-cal",
        )
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=account.user,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        baker.make(
            CalendarOwnership,
            organization=organization,
            calendar=calendar,
            membership_user_id=account.user_id,
            is_default=True,
        )

        with patch(
            "calendar_integration.services.calendar_adapters."
            "google_calendar_adapter.GoogleCalendarAdapter"
        ) as adapter_class:
            adapter = MagicMock()
            adapter.provider = CalendarProvider.GOOGLE
            del adapter.resolve_expression
            del adapter.get_source_expressions
            adapter_class.return_value = adapter
            adapter_class.from_service_account.return_value = adapter

            with patch.object(sync_calendar_task, "delay") as dispatched:
                resync_organization_calendars_task(organization_id=organization.pk)

        created_sync = CalendarSync.objects.get(calendar=calendar, organization=organization)
        assert created_sync.should_update_events is True
        dispatched.assert_called_once()

    def test_a_still_restricted_organization_is_a_no_op(self):
        """Defense in depth: a redelivery racing a further billing-state
        change (e.g. the org fell back into RESTRICTED again) must not run."""
        organization, calendar = TestRecoveryDispatchesAResync()._active_org_with_owned_calendar()

        with patch.object(sync_calendar_task, "delay") as dispatched:
            resync_organization_calendars_task(organization_id=organization.pk)

        dispatched.assert_not_called()
        assert not CalendarSync.objects.filter(calendar=calendar).exists()


# ---------------------------------------------------------------------------
# The resync genuinely reconciles drift, not just "resumes from here forward"
# ---------------------------------------------------------------------------


def _adapter_event(
    external_id: str,
    title: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id="reconcile-cal",
        title=title,
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        attendees=[],
        external_id=external_id,
        status="confirmed",  # type: ignore[arg-type]
        original_payload={"id": external_id},
    )


@pytest.mark.django_db
class TestResyncReconcilesDrift:
    def test_sync_pulls_in_changes_made_at_the_provider_while_paused(self):
        """The provider-side changes below stand in for whatever happened
        during the paused window: this is the same diff/merge engine
        ``request_calendar_sync`` -> ``sync_calendar_task`` ->
        ``CalendarService.sync_events`` drives for any sync, exercised here
        directly against a stale local row to prove a resync is a genuine
        reconciliation, not merely "sync resumes from here forward"."""
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id="reconcile-cal",
        )
        user = User.objects.create_user(email="reconcile@example.com", password="pw")  # noqa: S106
        Profile.objects.create(user=user)

        stale_block = baker.make(
            BlockedTime,
            calendar=calendar,
            organization=organization,
            start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0),
            timezone="UTC",
            reason="Stale (pre-restriction)",
            external_id="ext-drifted",
        )

        fake_adapter = MagicMock()
        fake_adapter.provider = CalendarProvider.GOOGLE
        fake_adapter.get_events.return_value = {
            "events": [
                _adapter_event(
                    "ext-drifted",
                    "Reconciled while restricted",
                    datetime.datetime(2026, 1, 1, 9, 30, tzinfo=datetime.UTC),
                    datetime.datetime(2026, 1, 1, 10, 30, tzinfo=datetime.UTC),
                )
            ],
            "next_sync_token": "tok-after-resync",
        }

        context = CalendarServiceContext(
            organization=organization,
            user_or_token=user,
            account=user,
            calendar_adapter=fake_adapter,
            calendar_permission_service=None,
            calendar_side_effects_service=None,
        )
        service = CalendarSyncService(context=context, calendar_cache={}, host=MagicMock())
        calendar_sync = baker.make(
            CalendarSync,
            calendar=calendar,
            organization=organization,
            start_datetime=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.UTC),
            end_datetime=datetime.datetime(2026, 1, 1, 23, 59, tzinfo=datetime.UTC),
            should_update_events=True,
            status=CalendarSyncStatus.IN_PROGRESS,
        )

        service._execute_calendar_sync(calendar_sync, sync_token=None)

        stale_block.refresh_from_db()
        # The stale local row is updated in place from the provider's data
        # (not duplicated) -- the same assertion shape
        # ``test_execute_calendar_sync_full_cycle_creates_updates_and_deletes``
        # uses to prove a reconciling update.
        assert stale_block.reason == "Reconciled while restricted"
        assert BlockedTime.objects.filter(calendar=calendar, external_id="ext-drifted").count() == 1

"""Webhook subscription lifecycle and webhook-triggered sync.

``CalendarWebhookService`` owns the webhook concern extracted from the
``CalendarService`` facade. It is a plain class (not a DI-container provider):
the facade constructs it (fresh per request, after authentication) feeding it
the shared :class:`CalendarServiceContext` so it never re-authenticates or
re-builds a calendar adapter (the perf guardrail). Everything it needs arrives
via the constructor:

- ``context`` — the immutable auth snapshot (organization, user_or_token,
  account, calendar_adapter, permission_service, side_effects_service). Read
  through ``self._context``; the auth guards in ``type_guards.py`` inspect the
  same ``organization`` / ``account`` / ``calendar_adapter`` attributes the
  context exposes, so behavior is byte-for-byte identical to the former methods.
- ``calendar_cache`` — the facade-owned, per-instance ``{(org_id, id): Calendar}``
  cache (the lru_cache multi-tenant fix from Phase 0). Shared so lookups are not
  duplicated across the facade and this service.
- ``host`` — the :class:`WebhookServiceHost` (in Phase 6 the facade itself).
  The webhook concern routes things back through it:

  - **sync triggering** (``request_calendar_sync``) — the sync concern, extracted
    in Phase 5; reached through the host so the facade's delegation seam is
    preserved and the existing test suite can patch it on the facade without
    changing this service.
  - **webhook-triggered sync** (``request_webhook_triggered_sync``) — defined on
    this service but ``process_webhook_notification`` routes it through the host so
    ``@patch.object(CalendarService, "request_webhook_triggered_sync")`` in the
    existing test suite intercepts the call.
  - **adapter-class lookup** (``_get_calendar_adapter_cls_for_provider``) — a
    static helper on the facade; routed here for a single implementation.
  - **write-adapter resolution** (``_get_write_adapter_for_calendar``) — the
    shared write-adapter helper on the facade.
  - **external-id calendar lookup** (``_get_calendar_by_external_id``) — uses the
    shared per-instance calendar cache (Phase 0 lru fix); routed through the host
    so cache sharing is preserved.

Organization-context in ``handle_webhook``: the facade's ``handle_webhook``
extracts the organization and writes ``self.organization = organization`` before
calling ``_get_webhook_service().handle_webhook()``.  Because
:meth:`CalendarService._build_context_snapshot` reads live facade attributes,
the freshly-constructed sub-service already has the correct organization in its
frozen context.  All downstream sub-service methods (including the fresh sub-service
instances the host creates for the ``request_webhook_triggered_sync`` callback) also
see it via ``_build_context_snapshot`` called at that point.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

from django.conf import settings
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    CalendarSyncTriggerSource,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import (
    ServiceNotAuthenticatedError,
    WebhookIgnoredError,
)
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
)
from calendar_integration.services.protocols.authenticated_calendar_service import (
    AuthenticatedCalendarService,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)
from calendar_integration.services.type_guards import (
    is_authenticated_calendar_service,
    is_initialized_or_authenticated_calendar_service,
)
from payments.exceptions import OverLimitError


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


logger = logging.getLogger(__name__)


class WebhookHealthStatus(TypedDict):
    """Health metrics for the webhook system of an organization."""

    total_subscriptions: int
    active_subscriptions: int
    expired_subscriptions: int
    expiring_soon_subscriptions: int
    recent_events_count: int
    failed_events_count: int
    success_rate: float


class WebhookServiceHost(Protocol):
    """The collaborator surface the webhook concern routes back to the facade for.

    Concerns not part of the webhook surface that stay on the facade (or the facade
    re-delegates from here):

    - **sync triggering** (``request_calendar_sync``) — the sync concern (Phase 5);
      reached through the host to keep one implementation and the call graph the
      existing test suite patches on the facade.
    - **webhook-triggered sync** (``request_webhook_triggered_sync``) — defined on
      this service but also on the facade; ``process_webhook_notification`` routes it
      through the host so ``@patch.object(CalendarService, "request_webhook_triggered_sync")``
      in the existing test suite intercepts the call.
    - **adapter-class lookup** (``_get_calendar_adapter_cls_for_provider``) — a
      static helper on the facade; routed here for a single implementation.
    - **write-adapter resolution** (``_get_write_adapter_for_calendar``) — the
      shared write-adapter helper on the facade.
    - **external-id calendar lookup** (``_get_calendar_by_external_id``) — uses the
      shared per-instance calendar cache (Phase 0 lru fix); routed through the host
      so cache sharing is preserved.

    In Phase 6 the facade supplies *itself*. Later phases may swap individual
    concerns without changing this service's call sites.
    """

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: CalendarSyncTriggerSource = CalendarSyncTriggerSource.MANUAL,
    ) -> CalendarSync | None: ...

    def request_webhook_triggered_sync(
        self,
        external_calendar_id: str,
        webhook_event: CalendarWebhookEvent,
        sync_window_hours: int = 24,
    ) -> CalendarSync | None: ...

    def _get_calendar_adapter_cls_for_provider(self, provider: CalendarProvider) -> Any: ...

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> CalendarAdapter | None: ...

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar: ...


class CalendarWebhookService:
    """Owns webhook subscription lifecycle and webhook-triggered sync."""

    def __init__(
        self,
        context: CalendarServiceContext,
        calendar_cache: dict[tuple[int, str | int], Calendar],
        host: WebhookServiceHost,
    ) -> None:
        self._context = context
        self._calendar_cache = calendar_cache
        # Phase 6 seam: sync triggering and shared adapter/lookup helpers are
        # reached through the host (the facade). See ``WebhookServiceHost``.
        self._host = host

    # ------------------------------------------------------------------
    # Webhook-triggered sync
    # ------------------------------------------------------------------

    def request_webhook_triggered_sync(
        self,
        external_calendar_id: str,
        webhook_event: CalendarWebhookEvent,
        sync_window_hours: int = 24,
    ) -> CalendarSync | None:
        """Request calendar sync triggered by webhook notification.

        Reuses existing request_calendar_sync with webhook-specific optimizations.

        Args:
            external_calendar_id: External calendar ID from webhook
            webhook_event: The webhook event that triggered this sync
            sync_window_hours: Hours around current time to sync

        Returns:
            CalendarSync instance if sync was triggered, None if skipped
        """
        now = datetime.datetime.now(tz=datetime.UTC)

        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise ValueError("Calendar service not properly initialized")
        # After the guard, organization is guaranteed non-None; narrow the type so
        # mypy can verify .organization.id access below.
        narrowed = cast("InitializedOrAuthenticatedCalendarService", context)

        # Find calendar by external ID
        try:
            calendar = Calendar.objects.get(
                organization_id=narrowed.organization.id,
                external_id=external_calendar_id,
                provider=webhook_event.provider,
            )
        except Calendar.DoesNotExist:
            logger.warning("Calendar not found for external_id: %s", external_calendar_id)
            return None

        # Check for recent syncs to prevent excessive syncing (deduplication)
        recent_sync = CalendarSync.objects.filter(
            calendar=calendar,
            created__gte=now - datetime.timedelta(minutes=5),
            status__in=[CalendarSyncStatus.IN_PROGRESS, CalendarSyncStatus.SUCCESS],
        ).first()

        if recent_sync:
            logger.info(
                "Skipping sync for calendar %s, recent sync exists: %s",
                calendar.id,
                recent_sync.id,
            )
            webhook_event.calendar_sync = recent_sync
            webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
            webhook_event.save()
            return recent_sync

        # Define sync window around current time
        start_datetime = now - datetime.timedelta(hours=sync_window_hours // 2)
        end_datetime = now + datetime.timedelta(hours=sync_window_hours // 2)

        # Use existing request_calendar_sync method via the host (the sync concern,
        # Phase 5 seam) so the facade's delegation path is preserved.
        calendar_sync = self._host.request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=True,  # Webhook implies changes, so update existing events
            trigger_source=CalendarSyncTriggerSource.WEBHOOK,
        )

        # Link webhook event to triggered sync
        webhook_event.calendar_sync = calendar_sync
        webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
        webhook_event.save()

        return calendar_sync

    # ------------------------------------------------------------------
    # Webhook subscription lifecycle
    # ------------------------------------------------------------------

    def create_calendar_webhook_subscription(
        self,
        calendar: Calendar,
        callback_url: str | None = None,
        expiration_hours: int = 24,
    ) -> CalendarWebhookSubscription:
        """Create webhook subscription using existing adapter methods.

        Works for both Google and Microsoft calendars.

        Args:
            calendar: Calendar to create subscription for
            callback_url: URL to receive webhook notifications (optional, will generate if not
                provided)
            expiration_hours: Hours until subscription expires

        Returns:
            CalendarWebhookSubscription instance

        Raises:
            ValueError: If calendar service not authenticated or provider not supported
        """
        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise ValueError("Calendar service not authenticated")

        # After the guard, calendar_adapter is guaranteed non-None; narrow the type so
        # mypy can verify .calendar_adapter access below.
        auth_context = cast("AuthenticatedCalendarService", context)
        if not auth_context.calendar_adapter:
            raise ValueError("Calendar adapter not available")

        # Generate callback URL if not provided
        if not callback_url:
            if calendar.provider == CalendarProvider.GOOGLE:
                callback_url = reverse(
                    "calendar_integration:google_webhook",
                    kwargs={"organization_id": calendar.organization_id},
                )
            elif calendar.provider == CalendarProvider.MICROSOFT:
                callback_url = reverse(
                    "calendar_integration:microsoft_webhook",
                    kwargs={"organization_id": calendar.organization_id},
                )
            else:
                raise ValueError(
                    f"Webhook subscriptions not supported for provider: {calendar.provider}"
                )

            # Convert to absolute URL if needed
            # In production, you might want to configure the domain via settings
            if callback_url.startswith("/"):
                domain = getattr(settings, "WEBHOOK_DOMAIN", "https://your-domain.com")
                callback_url = f"{domain.rstrip('/')}{callback_url}"

        # Use adapter-specific subscription creation
        if calendar.provider == CalendarProvider.GOOGLE:
            subscription_data = (
                auth_context.calendar_adapter.create_webhook_subscription_with_tracking(
                    resource_id=calendar.external_id,
                    callback_url=callback_url,
                    tracking_params={"ttl_seconds": expiration_hours * 3600},
                )
            )
        elif calendar.provider == CalendarProvider.MICROSOFT:
            subscription_data = (
                auth_context.calendar_adapter.create_webhook_subscription_with_tracking(
                    resource_id=calendar.external_id,
                    callback_url=callback_url,
                    tracking_params={"expiration_hours": expiration_hours},
                )
            )
        else:
            raise ValueError(
                f"Webhook subscriptions not supported for provider: {calendar.provider}"
            )

        # Calculate expiration datetime
        expiration_timestamp = subscription_data.get("expiration")
        expires_at = None
        if expiration_timestamp is not None:
            try:
                if calendar.provider == CalendarProvider.GOOGLE:
                    # Google returns expiration as milliseconds since epoch
                    expires_at = datetime.datetime.fromtimestamp(
                        int(expiration_timestamp) / 1000, tz=datetime.UTC
                    )
                elif calendar.provider == CalendarProvider.MICROSOFT:
                    # Microsoft returns expiration as ISO 8601 string
                    expires_at = datetime.datetime.fromisoformat(
                        expiration_timestamp.replace("Z", "+00:00")
                    )
            except (ValueError, TypeError):
                # Log or handle malformed expiration value gracefully
                expires_at = None
                # Log or handle malformed expiration value gracefully
                expires_at = None

        # Create tracking record
        webhook_subscription = CalendarWebhookSubscription.objects.create(
            calendar=calendar,
            organization_id=calendar.organization_id,
            provider=calendar.provider,
            external_subscription_id=subscription_data.get("subscription_id")
            or subscription_data.get("channel_id"),
            external_resource_id=subscription_data.get("resource_id", ""),
            callback_url=callback_url,
            channel_id=subscription_data.get("channel_id", ""),
            resource_uri=subscription_data.get("resource_uri", ""),
            verification_token=subscription_data.get("client_state")
            or subscription_data.get("channel_token")
            or "",
            expires_at=expires_at,
        )

        return webhook_subscription

    def process_webhook_notification(
        self,
        provider: str,
        calendar_external_id: str,
        headers: dict[str, str],
        payload: dict | str | None = None,
        validation_token: str | None = None,
    ) -> CalendarWebhookEvent | None:
        """Process incoming webhook notification using adapter validation.

        Returns CalendarWebhookEvent for notification

        Args:
            provider: Calendar provider (google, microsoft)
            headers: HTTP headers from webhook request
            payload: Webhook payload data
            validation_token: Validation token for subscription setup

        Returns:
            CalendarWebhookEvent for notifications

        Raises:
            ValueError: If validation fails or provider not supported
        """
        context = cast("BaseCalendarService", self._context)

        if not is_initialized_or_authenticated_calendar_service(context):
            # For webhook processing, we can proceed with limited functionality
            logger.warning(
                "Webhook received but calendar service not authenticated, "
                "webhook event recorded for later processing"
            )

        # Try to get calendar and adapter, but don't fail if not authenticated
        calendar = None
        calendar_adapter = None

        try:
            calendar = self._host._get_calendar_by_external_id(calendar_external_id)
            calendar_adapter = self._host._get_write_adapter_for_calendar(calendar)
        except (ServiceNotAuthenticatedError, Calendar.DoesNotExist):
            # Calendar not found or not authenticated - we'll still record the webhook event
            pass
        except OverLimitError as exc:
            # The organization lost the calendar's provider entitlement. Unlike the
            # interactive REST/GraphQL callers of _get_write_adapter_for_calendar (which
            # keep the 402 -- a user asking to connect a calendar should be told why),
            # this webhook caller has no user to tell: Google/Microsoft's server-to-server
            # push has nowhere to route a 402 to, and would just retry against a 500 until
            # the channel expires. Degrade to the static-validation fallback below and
            # still record the event, mirroring _authenticate_or_skip's reasoning for the
            # scheduled sync tasks.
            logger.info(
                "Skipping write-adapter resolution for webhook on calendar %s: %s",
                calendar_external_id,
                exc.as_error_body()["detail"],
            )

        # Handle provider-specific validation/parsing
        # Use static validation if we don't have an authenticated adapter
        if calendar_adapter:
            parsed_data = calendar_adapter.validate_webhook_notification(
                headers, json.dumps(payload) if payload else ""
            )
        else:
            # Use static validation method
            calendar_adapter_cls = self._host._get_calendar_adapter_cls_for_provider(
                CalendarProvider(provider)
            )
            parsed_data = calendar_adapter_cls.validate_webhook_notification_static(
                headers, json.dumps(payload) if payload else ""
            )

        # The organization is accessed below — cast to the narrowed type for mypy.
        # The BaseCalendarService cast above is for the auth guards (which use hasattr);
        # after the org check we need the attribute typed.
        org_context = cast("InitializedOrAuthenticatedCalendarService", context)
        if not org_context.organization:
            raise ValueError("Organization context not set on calendar service")

        # Create webhook event record
        webhook_event = CalendarWebhookEvent.objects.create(
            organization_id=org_context.organization.id,
            provider=provider,
            event_type=parsed_data.get("event_type", "unknown"),
            external_calendar_id=parsed_data.get("calendar_id", ""),
            external_event_id=parsed_data.get("event_id", ""),
            raw_payload=payload if isinstance(payload, dict) else {"raw": str(payload or "")},
            headers=headers,
        )

        # Trigger calendar sync only if service is authenticated
        # For webhook processing, we record the event even if sync can't be triggered immediately
        try:
            if is_authenticated_calendar_service(context, raise_error=False):
                # Route through host so that @patch.object(CalendarService,
                # "request_webhook_triggered_sync") patches in the existing test suite are
                # intercepted.  The facade's request_webhook_triggered_sync calls
                # _get_webhook_service().request_webhook_triggered_sync(), which is correct when
                # unpatched; when patched on the facade, the mock fires first.
                calendar_sync = self._host.request_webhook_triggered_sync(
                    external_calendar_id=parsed_data["calendar_id"], webhook_event=webhook_event
                )

                if calendar_sync:
                    logger.info(
                        "Webhook triggered sync %s for calendar %s",
                        calendar_sync.id,
                        parsed_data["calendar_id"],
                    )
                    return webhook_event
                else:
                    webhook_event.processing_status = IncomingWebhookProcessingStatus.IGNORED
                    webhook_event.save()
            else:
                # Service not authenticated - just record the webhook event for later processing
                logger.warning(
                    "Webhook received but calendar service not authenticated, "
                    "webhook event recorded for later processing"
                )
                webhook_event.processing_status = IncomingWebhookProcessingStatus.PENDING
                webhook_event.save()

        except Exception as e:
            webhook_event.processing_status = IncomingWebhookProcessingStatus.FAILED
            webhook_event.save()
            logger.exception("Failed to process webhook: %s", e)
            # Don't re-raise the exception - webhook event is recorded

        return webhook_event

    def handle_webhook(
        self, provider: CalendarProvider, request: HttpRequest
    ) -> CalendarWebhookEvent | None:
        """Handle calendar webhook processing with organization context.

        The facade's ``handle_webhook`` extracts the organization and writes
        ``self.organization`` *before* constructing this sub-service instance, so
        ``self._context.organization`` is already set when this method is called.
        This method only needs to parse the provider-specific headers and delegate
        to ``process_webhook_notification``.

        Args:
            provider: Calendar provider enum
            request: HttpRequest object containing webhook data

        Returns:
            CalendarWebhookEvent if processed successfully, None for sync notifications

        Raises:
            ValueError: If webhook validation fails or organization not found
            Exception: If processing fails
        """
        calendar_adapter_cls = self._host._get_calendar_adapter_cls_for_provider(provider)

        headers = calendar_adapter_cls.parse_webhook_headers(request.headers)
        calendar_external_id = (
            calendar_adapter_cls.extract_calendar_external_id_from_webhook_request(request)
        )

        # Process the webhook notification
        try:
            return self.process_webhook_notification(
                provider=provider,
                calendar_external_id=calendar_external_id,
                headers=headers,
            )
        except WebhookIgnoredError:
            return None

    def list_webhook_subscriptions(self) -> QuerySet[CalendarWebhookSubscription]:
        """List all active webhook subscriptions for the organization.

        Returns:
            QuerySet of CalendarWebhookSubscription objects for the organization

        Raises:
            ValueError: If organization is not set
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            raise ValueError("Organization must be set")

        return CalendarWebhookSubscription.objects.filter(
            organization=context.organization, is_active=True
        ).select_related("calendar")

    def delete_webhook_subscription(self, subscription_id: int) -> bool:
        """Delete a webhook subscription from provider and database.

        Args:
            subscription_id: ID of the subscription to delete

        Returns:
            True if successfully deleted, False if subscription not found

        Raises:
            ValueError: If organization is not set or subscription not found
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            raise ValueError("Organization must be set")

        try:
            subscription = CalendarWebhookSubscription.objects.get(
                id=subscription_id, organization=context.organization
            )
        except CalendarWebhookSubscription.DoesNotExist:
            return False

        # TODO: Add provider-specific subscription deletion when implementing
        # Google Calendar and Microsoft Graph subscription deletion APIs
        # For now, just mark as inactive
        subscription.is_active = False
        subscription.save(update_fields=["is_active", "modified"])

        return True

    def refresh_webhook_subscription(
        self, subscription_id: int
    ) -> CalendarWebhookSubscription | None:
        """Refresh/renew a webhook subscription with the provider.

        Args:
            subscription_id: ID of the subscription to refresh

        Returns:
            Updated CalendarWebhookSubscription if successful, None if not found

        Raises:
            ValueError: If organization is not set
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            raise ValueError("Organization must be set")

        try:
            subscription = CalendarWebhookSubscription.objects.get(
                id=subscription_id, organization=context.organization, is_active=True
            )
        except CalendarWebhookSubscription.DoesNotExist:
            return None

        # TODO: Implement provider-specific subscription renewal
        # For now, extend expiration by default duration based on provider
        now = datetime.datetime.now(tz=datetime.UTC)
        if subscription.provider == CalendarProvider.GOOGLE:
            # Google allows max 7 days (604800 seconds)
            new_expiration = now + datetime.timedelta(days=7)
        elif subscription.provider == CalendarProvider.MICROSOFT:
            # Microsoft allows max ~70 hours (4230 minutes)
            new_expiration = now + datetime.timedelta(minutes=4230)
        else:
            # Default to 1 day for other providers
            new_expiration = now + datetime.timedelta(days=1)

        subscription.expires_at = new_expiration
        subscription.save(update_fields=["expires_at", "modified"])

        return subscription

    def get_webhook_health_status(self) -> WebhookHealthStatus:
        """Get webhook system health status for the organization.

        Returns:
            WebhookHealthStatus with webhook health metrics

        Raises:
            ValueError: If organization is not set
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            raise ValueError("Organization must be set")

        organization = context.organization

        # Time boundaries
        now = datetime.datetime.now(tz=datetime.UTC)
        twenty_four_hours_ago = now - datetime.timedelta(hours=24)
        expiring_soon_threshold = now + datetime.timedelta(hours=24)

        # Subscription counts
        subscriptions_qs = CalendarWebhookSubscription.objects.filter(organization=organization)
        total_subscriptions = subscriptions_qs.count()
        active_subscriptions = subscriptions_qs.filter(is_active=True).count()
        expired_subscriptions = subscriptions_qs.filter(is_active=True, expires_at__lt=now).count()
        expiring_soon_subscriptions = subscriptions_qs.filter(
            is_active=True,
            expires_at__gte=now,
            expires_at__lte=expiring_soon_threshold,
        ).count()

        # Event counts in last 24 hours
        events_qs = CalendarWebhookEvent.objects.filter(
            organization=organization, created__gte=twenty_four_hours_ago
        )
        recent_events_count = events_qs.count()
        failed_events_count = events_qs.filter(
            processing_status=IncomingWebhookProcessingStatus.FAILED
        ).count()

        # Calculate success rate
        if recent_events_count > 0:
            success_rate = ((recent_events_count - failed_events_count) / recent_events_count) * 100
        else:
            success_rate = 100.0

        return WebhookHealthStatus(
            {
                "total_subscriptions": total_subscriptions,
                "active_subscriptions": active_subscriptions,
                "expired_subscriptions": expired_subscriptions,
                "expiring_soon_subscriptions": expiring_soon_subscriptions,
                "recent_events_count": recent_events_count,
                "failed_events_count": failed_events_count,
                "success_rate": success_rate,
            }
        )

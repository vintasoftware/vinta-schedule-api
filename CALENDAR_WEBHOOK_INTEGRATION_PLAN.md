# Calendar Webhook Integration Development Plan

## Overview
This plan outlines the development of webhook endpoints to receive calendar event notifications from Google Calendar and Microsoft Outlook, triggering automatic event synchronization. The implementation leverages and enhances the existing `CalendarService` and calendar adapter architecture, reusing the proven `request_calendar_sync()` method and extending adapters with webhook-specific functionality.

## Architecture Approach
- **Extend existing CalendarService**: Add webhook-specific methods that reuse `request_calendar_sync()`
- **Enhance calendar adapters**: Add webhook validation methods to `GoogleCalendarAdapter` and `MSOutlookCalendarAdapter`
- **Minimal new services**: Only create webhook view classes, avoid duplicating existing sync logic
- **Consistent patterns**: Follow the existing dependency injection and organization scoping patterns

The plan is divided into deployable phases to minimize risk and enable incremental testing.

## Phase 1: Core Webhook Infrastructure 🏗️

### Goal
Establish the foundational webhook receiving infrastructure with proper security and validation.

### Deliverables

#### 1.1 Webhook Models Enhancement
- **File**: `calendar_integration/models.py`
- **Changes**:
  - Add `CalendarWebhookSubscription` model to track active subscriptions
  - Add `CalendarWebhookEvent` model to log incoming webhook events

**CalendarWebhookSubscription Model:**
```python
class CalendarWebhookSubscription(OrganizationModel):
    """Tracks active webhook subscriptions for calendars."""
    calendar = OrganizationForeignKey(Calendar, on_delete=models.CASCADE, related_name='webhook_subscriptions')
    provider = models.CharField(max_length=50, choices=CalendarProvider.choices)
    external_subscription_id = models.CharField(max_length=255, help_text="Provider's subscription ID")
    external_resource_id = models.CharField(max_length=255, help_text="Provider's resource/calendar ID")
    callback_url = models.URLField(max_length=500)
    channel_id = models.CharField(max_length=255, null=True, blank=True, help_text="Google Calendar channel ID")
    resource_uri = models.CharField(max_length=500, null=True, blank=True, help_text="Google Calendar resource URI")
    verification_token = models.CharField(max_length=255, null=True, blank=True, help_text="Webhook verification token")
    expires_at = models.DateTimeField(null=True, blank=True, help_text="When subscription expires")
    is_active = models.BooleanField(default=True)
    last_notification_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('organization', 'calendar', 'provider')]
        indexes = [
            models.Index(fields=['provider', 'external_subscription_id']),
            models.Index(fields=['expires_at']),
            models.Index(fields=['is_active', 'created_at']),
        ]

    def __str__(self):
        return f"WebhookSubscription({self.provider}:{self.calendar.name})"
```

**CalendarWebhookEvent Model:**
```python
class CalendarWebhookEvent(OrganizationModel):
    """Logs incoming webhook notifications for debugging and monitoring."""
    subscription = OrganizationForeignKey(
        CalendarWebhookSubscription, 
        on_delete=models.CASCADE, 
        related_name='webhook_events',
        null=True, blank=True  # In case subscription is deleted but we want to keep logs
    )
    provider = models.CharField(max_length=50, choices=CalendarProvider.choices)
    event_type = models.CharField(max_length=100, help_text="Type of webhook event (created, updated, deleted)")
    external_calendar_id = models.CharField(max_length=255, help_text="External calendar ID from webhook")
    external_event_id = models.CharField(max_length=255, null=True, blank=True, help_text="External event ID if available")
    raw_payload = models.JSONField(help_text="Full webhook payload for debugging")
    headers = models.JSONField(default=dict, help_text="Request headers")
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_status = models.CharField(
        max_length=50, 
        choices=IncomingWebhookProcessingStatus.choices,
        default=IncomingWebhookProcessingStatus.PENDING
    )
    calendar_sync = OrganizationForeignKey(
        'CalendarSync', 
        on_delete=models.SET_NULL, 
        null=True, blank=True,
        help_text="Associated calendar sync if triggered"
    )

    class Meta:
        indexes = [
            models.Index(fields=['provider', 'created_at']),
            models.Index(fields=['processing_status', 'created_at']),
            models.Index(fields=['external_calendar_id', 'created_at']),
        ]

    def __str__(self):
        return f"WebhookEvent({self.provider}:{self.event_type}:{self.created_at})"

    @property
    def sync_triggered(self) -> bool:
        """Whether calendar sync was triggered - derived from calendar_sync field."""
        return self.calendar_sync is not None

    @property 
    def error_message(self) -> str | None:
        """Error message from associated calendar sync if failed."""
        if self.calendar_sync and self.calendar_sync.status == CalendarSyncStatus.FAILED:
            return self.calendar_sync.error_message
        return None
```

#### 1.2 Webhook Constants & Exceptions
- **File**: `calendar_integration/constants.py`
- **Changes**:
  - Add `IncomingWebhookProcessingStatus` choices

```python
class IncomingWebhookProcessingStatus(TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed" 
    FAILED = "failed", "Failed"
    IGNORED = "ignored", "Ignored"
```

- **File**: `calendar_integration/exceptions.py`
- **Changes**:
  - Add `WebhookValidationError`
  - Add `WebhookAuthenticationError`

#### 1.3 Incoming Webhook Service
- **File**: `calendar_integration/services/incoming_webhook_service.py`
- **Changes**:
  - Create `CalendarIncomingWebhookService` class
  - Implement webhook signature validation logic
  - Add logging and error handling patterns

#### 1.4 Database Migrations
- **Files**: `calendar_integration/migrations/`
- **Changes**:
  - Create migrations for new webhook models
  - Add indexes for performance (provider, calendar_id, created_at)

### Testing Strategy
- Unit tests for webhook models and base service
- Integration tests for database operations
- Mock webhook payload validation

### Deployment Notes
- Can be deployed independently
- No breaking changes to existing functionality
- Requires database migration

---

## Phase 2: Google Calendar Webhook Receiver 🟡

### Goal
Implement complete Google Calendar webhook receiving functionality with proper validation and security.

### Deliverables

#### 2.1 Google Webhook Validation (Enhanced GoogleCalendarAdapter)
- **File**: `calendar_integration/services/calendar_adapters/google_calendar_adapter.py`
- **Changes**:
  - Add webhook validation methods to existing adapter
  - Leverage existing Google API client and authentication
  - Add webhook-specific helper methods

```python
def validate_webhook_notification(
    self, 
    headers: dict[str, str], 
    body: bytes | str,
    expected_channel_id: str | None = None
) -> dict[str, Any]:
    """
    Validate incoming Google Calendar webhook notification.
    Returns parsed webhook data if valid.
    """
    # Google Calendar webhooks have specific headers
    resource_id = headers.get('X-Goog-Resource-ID')
    resource_uri = headers.get('X-Goog-Resource-URI')  
    resource_state = headers.get('X-Goog-Resource-State')
    channel_id = headers.get('X-Goog-Channel-ID')
    channel_token = headers.get('X-Goog-Channel-Token')
    
    if not all([resource_id, resource_uri, resource_state, channel_id]):
        raise ValueError("Missing required Google webhook headers")
    
    # Validate channel ID if provided
    if expected_channel_id and channel_id != expected_channel_id:
        raise ValueError(f"Channel ID mismatch: expected {expected_channel_id}, got {channel_id}")
    
    # Parse resource URI to extract calendar ID
    # Format: https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events
    import re
    calendar_id_match = re.search(r'/calendars/([^/]+)/events', resource_uri)
    calendar_id = calendar_id_match.group(1) if calendar_id_match else None
    
    if not calendar_id:
        raise ValueError(f"Could not extract calendar ID from resource URI: {resource_uri}")
    
    return {
        'provider': 'google',
        'calendar_id': calendar_id,
        'resource_id': resource_id,
        'resource_uri': resource_uri,
        'resource_state': resource_state,
        'channel_id': channel_id,
        'channel_token': channel_token,
        'event_type': resource_state  # 'sync', 'exists', 'not_exists'
    }

def create_webhook_subscription_with_tracking(
    self, 
    calendar_id: str, 
    callback_url: str,
    channel_id: str | None = None,
    ttl_seconds: int = 3600
) -> dict[str, Any]:
    """
    Enhanced version of subscribe_to_calendar_events that returns subscription details.
    """
    import uuid
    
    if not channel_id:
        channel_id = f"calendar-{calendar_id}-{uuid.uuid4().hex[:8]}"
    
    body = {
        "id": channel_id,
        "type": "web_hook", 
        "address": callback_url,
        "params": {
            "ttl": ttl_seconds,
        },
    }
    
    write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
    response = self.client.events().watch(calendarId=calendar_id, body=body).execute()
    
    return {
        'channel_id': response.get('id'),
        'resource_id': response.get('resourceId'), 
        'resource_uri': response.get('resourceUri'),
        'expiration': response.get('expiration'),
        'calendar_id': calendar_id,
        'callback_url': callback_url
    }
```

#### 2.2 Google Webhook Views
- **File**: `calendar_integration/views/webhook_views.py`
- **Changes**:
  - Create `GoogleCalendarWebhookView` (POST endpoint)
  - Implement CSRF exemption and custom authentication
  - Add proper error handling and logging
  - Support both verification challenges and actual notifications

#### 2.3 URL Routing
- **File**: `calendar_integration/routes.py`
- **Changes**:
  - Add route for `/webhooks/google-calendar/`
  - Ensure proper URL pattern registration

#### 2.4 Integration with Calendar Sync (Enhanced CalendarService)
- **File**: `calendar_integration/services/calendar_service.py`
- **Changes**:
  - Add `request_webhook_triggered_sync()` method that reuses existing `request_calendar_sync()`
  - Add webhook deduplication and rate limiting logic
  - Add calendar lookup by external ID

```python
def request_webhook_triggered_sync(
    self,
    external_calendar_id: str,
    webhook_event: CalendarWebhookEvent,
    sync_window_hours: int = 24
) -> CalendarSync | None:
    """
    Request calendar sync triggered by webhook notification.
    Reuses existing request_calendar_sync with webhook-specific optimizations.
    """
    if not is_initialized_or_authenticated_calendar_service(self):
        raise ValueError("Calendar service not properly initialized")

    # Find calendar by external ID
    try:
        calendar = Calendar.objects.get(
            organization_id=self.organization.id,
            external_id=external_calendar_id,
            provider=webhook_event.provider
        )
    except Calendar.DoesNotExist:
        logger.warning(f"Calendar not found for external_id: {external_calendar_id}")
        return None

    # Check for recent syncs to prevent excessive syncing (deduplication)
    recent_sync = CalendarSync.objects.filter(
        calendar=calendar,
        created__gte=timezone.now() - timedelta(minutes=5),
        status__in=[CalendarSyncStatus.IN_PROGRESS, CalendarSyncStatus.SUCCESS]
    ).first()
    
    if recent_sync:
        logger.info(f"Skipping sync for calendar {calendar.id}, recent sync exists: {recent_sync.id}")
        webhook_event.calendar_sync = recent_sync
        webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
        webhook_event.save()
        return recent_sync

    # Define sync window around current time
    now = timezone.now()
    start_datetime = now - timedelta(hours=sync_window_hours // 2)
    end_datetime = now + timedelta(hours=sync_window_hours // 2)

    # Use existing request_calendar_sync method
    calendar_sync = self.request_calendar_sync(
        calendar=calendar,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        should_update_events=True  # Webhook implies changes, so update existing events
    )

    # Link webhook event to triggered sync
    webhook_event.calendar_sync = calendar_sync
    webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
    webhook_event.save()

    return calendar_sync
```

### Testing Strategy
- Mock Google webhook payloads for testing
- End-to-end tests with test calendar subscriptions
- Performance tests for high-frequency webhooks
- Security tests for payload validation

### Deployment Notes
- Requires Phase 1 to be deployed first
- New endpoint will be available but not yet used
- No impact on existing calendar sync functionality
- Need to configure webhook URL in Google Calendar subscriptions

---

## Phase 3: Microsoft Outlook Webhook Receiver 🔵

### Goal
Implement Microsoft Graph webhook receiving functionality with validation and authentication.

### Deliverables

#### 3.1 Microsoft Webhook Validation (Enhanced MSOutlookCalendarAdapter)
- **File**: `calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py`
- **Changes**:
  - Add webhook validation methods to existing adapter
  - Leverage existing MS Graph API client
  - Add Microsoft-specific webhook helper methods

```python
def validate_webhook_notification(
    self, 
    validation_token: str | None = None,
    payload: dict | None = None
) -> dict[str, Any] | str:
    """
    Validate Microsoft Graph webhook notification or validation request.
    
    Microsoft webhooks work in two modes:
    1. Validation: Returns validation_token for subscription setup
    2. Notification: Processes actual event notifications
    """
    
    # Handle validation request (subscription setup)
    if validation_token:
        logger.info(f"Microsoft webhook validation request received: {validation_token}")
        return validation_token  # Return plain text validation token
    
    # Handle notification payload
    if not payload or 'value' not in payload:
        raise ValueError("Invalid Microsoft Graph webhook payload")
    
    notifications = payload['value']
    processed_notifications = []
    
    for notification in notifications:
        # Extract notification details
        subscription_id = notification.get('subscriptionId')
        change_type = notification.get('changeType')  # created, updated, deleted
        resource = notification.get('resource')  # e.g., "/me/events/eventId"
        client_state = notification.get('clientState')  # Our verification token
        
        # Parse calendar and event IDs from resource
        # Format: "/me/events/{eventId}" or "/me/calendars/{calendarId}/events/{eventId}"
        calendar_id = 'primary'  # Default for "/me/events"
        event_id = None
        
        import re
        if '/calendars/' in resource:
            match = re.search(r'/calendars/([^/]+)/events/([^/]+)', resource)
            if match:
                calendar_id, event_id = match.groups()
        elif '/events/' in resource:
            match = re.search(r'/events/([^/]+)', resource)
            if match:
                event_id = match.group(1)
        
        processed_notifications.append({
            'provider': 'microsoft',
            'subscription_id': subscription_id,
            'change_type': change_type,
            'calendar_id': calendar_id,
            'event_id': event_id,
            'resource': resource,
            'client_state': client_state,
            'event_type': change_type  # created, updated, deleted
        })
    
    return {
        'provider': 'microsoft',
        'notifications': processed_notifications
    }

def create_webhook_subscription_with_tracking(
    self,
    calendar_id: str,
    callback_url: str,
    client_state: str | None = None,
    expiration_hours: int = 24
) -> dict[str, Any]:
    """
    Enhanced version of subscribe_to_calendar_events that returns subscription details.
    """
    import uuid
    from datetime import datetime, timedelta
    
    if not client_state:
        client_state = f"vinta-schedule-{uuid.uuid4().hex[:16]}"
    
    # Calculate expiration (Microsoft max is 4230 minutes = ~70 hours)
    expiration = datetime.utcnow() + timedelta(hours=min(expiration_hours, 70))
    
    try:
        subscription = self.client.subscribe_to_calendar_events(
            calendar_id=calendar_id,
            notification_url=callback_url,
            change_types=["created", "updated", "deleted"],
            client_state=client_state,
            expiration_datetime=expiration.isoformat() + "Z"
        )
        
        return {
            'subscription_id': subscription.get('id'),
            'resource': subscription.get('resource'),
            'calendar_id': calendar_id,
            'callback_url': callback_url,
            'client_state': client_state,
            'expiration': expiration,
            'change_types': subscription.get('changeType', 'created,updated,deleted').split(',')
        }
    except MSGraphAPIError as e:
        raise ValueError(f"Failed to create Microsoft webhook subscription: {e}") from e
```

#### 3.2 Microsoft Webhook Views (Same as Google)
- **File**: `calendar_integration/views/webhook_views.py` (extend existing)
- **Changes**:
  - Create `MicrosoftCalendarWebhookView` (POST endpoint)
  - Handle both validation and notification scenarios
  - Use enhanced adapter methods

#### 3.3 URL Routing Enhancement
- **File**: `calendar_integration/routes.py`
- **Changes**:
  - Add route for `/webhooks/microsoft-calendar/`

#### 3.4 Enhanced CalendarService Integration
- **File**: `calendar_integration/services/calendar_service.py`
- **Changes**:
  - Use the same `request_webhook_triggered_sync()` method for Microsoft webhooks
  - Add Microsoft-specific subscription management methods that leverage existing adapter methods

### Testing Strategy
- Mock Microsoft Graph webhook payloads
- Test subscription validation flow
- Test webhook authentication and validation
- Integration tests with calendar sync

### Deployment Notes
- Requires Phases 1-2 to be deployed first
- Independent of Google Calendar webhook functionality
- Requires Microsoft Graph webhook URL configuration
- May need subscription renewal task scheduling

---

## Phase 4: Enhanced Webhook Management & Monitoring 📊

### Goal
Add comprehensive webhook management, monitoring, and reliability features.

### Deliverables

#### 4.1 Webhook Management API
- **File**: `calendar_integration/views/webhook_management_views.py`
- **Changes**:
  - Create webhook subscription management endpoints
  - Add endpoints to list active subscriptions
  - Add endpoints to manually trigger webhook setup/teardown
  - Include subscription health checking

#### 4.2 Webhook Analytics & Monitoring
- **File**: `calendar_integration/services/webhook_analytics_service.py`
- **Changes**:
  - Track webhook delivery success rates
  - Monitor webhook latency and performance
  - Add webhook failure alerting
  - Implement webhook retry logic for failed processing

#### 4.3 Admin Interface Enhancement
- **File**: `calendar_integration/admin.py`
- **Changes**:
  - Add webhook subscription admin interface
  - Add webhook event logging admin interface
  - Include filtering and search capabilities

#### 4.4 Webhook Testing Tools
- **File**: `calendar_integration/management/commands/test_webhook.py`
- **Changes**:
  - Create management command for webhook testing
  - Add webhook payload simulation tools
  - Include subscription health checking commands

### Testing Strategy
- Integration tests for management API
- Performance tests for webhook processing
- End-to-end tests with real provider webhooks
- Admin interface functional tests

### Deployment Notes
- Enhances existing webhook functionality
- Provides operational visibility and control
- Non-breaking addition to existing features

---

## Phase 5: Advanced Features & Optimization 🚀

### Goal
Add advanced webhook features, performance optimizations, and production readiness enhancements.

### Deliverables

#### 5.1 Webhook Rate Limiting & Batching
- **File**: `calendar_integration/services/webhook_rate_limiter.py`
- **Changes**:
  - Implement webhook rate limiting to prevent API abuse
  - Add webhook batching for high-frequency events
  - Implement intelligent sync scheduling (debouncing)

#### 5.2 Webhook Security Enhancements
- **File**: `calendar_integration/middleware/webhook_security.py`
- **Changes**:
  - Add IP allowlisting for webhook sources
  - Implement webhook replay attack protection
  - Add additional security headers and validation

#### 5.3 Performance Optimizations
- **File**: `calendar_integration/tasks/webhook_tasks.py`
- **Changes**:
  - Move webhook processing to async Celery tasks
  - Implement webhook queue prioritization
  - Add bulk calendar sync operations

#### 5.4 Webhook Reliability Features
- **File**: `calendar_integration/services/webhook_reliability_service.py`
- **Changes**:
  - Implement webhook failure recovery mechanisms
  - Add automatic subscription renewal for expired webhooks
  - Include webhook health monitoring and alerting

### Testing Strategy
- Load testing for high-volume webhook scenarios
- Security penetration testing
- Reliability and failover testing
- Performance benchmarking

### Deployment Notes
- Performance and reliability improvements
- Requires careful monitoring during deployment
- May require infrastructure scaling considerations

---

## Cross-Cutting Concerns

### Security Considerations
- Webhook signature validation for all providers
- CSRF exemption handling
- Rate limiting and DDoS protection
- Secure credential management

### Performance Considerations
- Asynchronous webhook processing
- Database query optimization
- Caching strategies for frequent lookups
- Webhook deduplication and batching

### Monitoring & Observability
- Webhook delivery metrics
- Error rate monitoring
- Performance dashboards
- Alert configurations

### Documentation Requirements
- API documentation for webhook endpoints
- Provider-specific webhook setup guides
- Troubleshooting documentation
- Architecture decision records

---

## Technical Implementation Details

### Google Calendar Webhook Structure
```json
{
  "kind": "api#channel",
  "id": "channel-id",
  "resourceId": "resource-id",
  "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
  "token": "verification-token",
  "expiration": "1234567890000",
  "type": "web_hook",
  "address": "https://your-app.com/webhooks/google-calendar/"
}
```

### Microsoft Graph Webhook Structure
```json
{
  "value": [
    {
      "id": "subscription-id",
      "changeType": "created",
      "clientState": "client-state-token",
      "resource": "/me/events/event-id",
      "subscriptionExpirationDateTime": "2023-12-31T23:59:59Z",
      "subscriptionId": "subscription-id",
      "tenantId": "tenant-id"
    }
  ]
}
```

### Webhook Endpoint URLs
- **Google Calendar**: `POST /api/webhooks/google-calendar/`
- **Microsoft Calendar**: `POST /api/webhooks/microsoft-calendar/`
- **Webhook Management**: `GET/POST/PUT/DELETE /api/webhook-subscriptions/`

### Enhanced CalendarService Methods

#### Webhook Subscription Management
```python
def create_calendar_webhook_subscription(
    self,
    calendar: Calendar,
    callback_url: str,
    expiration_hours: int = 24
) -> CalendarWebhookSubscription:
    """
    Create webhook subscription using existing adapter methods.
    Works for both Google and Microsoft calendars.
    """
    if not is_authenticated_calendar_service(self):
        raise ValueError("Calendar service not authenticated")

    if not self.calendar_adapter:
        raise ValueError("Calendar adapter not available")

    # Use adapter-specific subscription creation
    if calendar.provider == CalendarProvider.GOOGLE:
        subscription_data = self.calendar_adapter.create_webhook_subscription_with_tracking(
            calendar_id=calendar.external_id,
            callback_url=callback_url,
            ttl_seconds=expiration_hours * 3600
        )
    elif calendar.provider == CalendarProvider.MICROSOFT:
        subscription_data = self.calendar_adapter.create_webhook_subscription_with_tracking(
            calendar_id=calendar.external_id,
            callback_url=callback_url,
            expiration_hours=expiration_hours
        )
    else:
        raise ValueError(f"Webhook subscriptions not supported for provider: {calendar.provider}")

    # Create tracking record
    webhook_subscription = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization_id=calendar.organization_id,
        provider=calendar.provider,
        external_subscription_id=subscription_data.get('subscription_id') or subscription_data.get('channel_id'),
        external_resource_id=subscription_data.get('resource_id', ''),
        callback_url=callback_url,
        channel_id=subscription_data.get('channel_id'),
        resource_uri=subscription_data.get('resource_uri', ''),
        verification_token=subscription_data.get('client_state') or subscription_data.get('channel_token'),
        expires_at=subscription_data.get('expiration')
    )

    return webhook_subscription

def process_webhook_notification(
    self, 
    provider: str,
    headers: dict[str, str],
    payload: dict | str,
    validation_token: str | None = None
) -> CalendarWebhookEvent | str:
    """
    Process incoming webhook notification using adapter validation.
    Returns CalendarWebhookEvent for notifications or validation token for validation requests.
    """
    if not is_initialized_or_authenticated_calendar_service(self):
        raise ValueError("Calendar service not initialized")

    # Handle provider-specific validation/parsing
    if provider == 'google':
        if not self.calendar_adapter or self.calendar_adapter.provider != 'google':
            # Initialize Google adapter for validation (we might not have it authenticated)
            parsed_data = GoogleCalendarAdapter.validate_webhook_notification_static(headers, payload)
        else:
            parsed_data = self.calendar_adapter.validate_webhook_notification(headers, payload)
            
    elif provider == 'microsoft':
        if validation_token:
            return validation_token  # Return validation token for subscription setup
        
        if not self.calendar_adapter or self.calendar_adapter.provider != 'microsoft':
            parsed_data = MSOutlookCalendarAdapter.validate_webhook_notification_static(payload=payload)
        else:
            parsed_data = self.calendar_adapter.validate_webhook_notification(payload=payload)
    else:
        raise ValueError(f"Unsupported webhook provider: {provider}")

    # Create webhook event record
    webhook_event = CalendarWebhookEvent.objects.create(
        organization_id=self.organization.id,
        provider=provider,
        event_type=parsed_data.get('event_type', 'unknown'),
        external_calendar_id=parsed_data.get('calendar_id', ''),
        external_event_id=parsed_data.get('event_id'),
        raw_payload=payload if isinstance(payload, dict) else {'raw': str(payload)},
        headers=headers
    )

    # Trigger calendar sync
    try:
        calendar_sync = self.request_webhook_triggered_sync(
            external_calendar_id=parsed_data['calendar_id'],
            webhook_event=webhook_event
        )
        
        if calendar_sync:
            logger.info(f"Webhook triggered sync {calendar_sync.id} for calendar {parsed_data['calendar_id']}")
        else:
            webhook_event.processing_status = IncomingWebhookProcessingStatus.IGNORED
            webhook_event.save()
            
    except Exception as e:
        webhook_event.processing_status = IncomingWebhookProcessingStatus.FAILED
        webhook_event.save()
        logger.error(f"Failed to process webhook: {e}", exc_info=True)

    return webhook_event
```

---

## Deployment Strategy

### Prerequisites
- Celery task queue configured and running
- Redis/database performance monitoring
- Webhook URL endpoint accessibility from providers
- Provider-specific webhook configuration

### Environment Variables
```bash
# Google Calendar Webhook Settings
GOOGLE_CALENDAR_WEBHOOK_SECRET=your-webhook-secret
GOOGLE_CALENDAR_WEBHOOK_TOKEN=verification-token

# Microsoft Graph Webhook Settings
MICROSOFT_GRAPH_WEBHOOK_CLIENT_STATE=client-state-token
MICROSOFT_GRAPH_NOTIFICATION_URL=https://your-app.com/api/webhooks/microsoft-calendar/

# Webhook Security
WEBHOOK_RATE_LIMIT_PER_MINUTE=100
WEBHOOK_MAX_PAYLOAD_SIZE=1048576  # 1MB
```

### Rollback Plans
- Each phase can be independently rolled back
- Database migrations include reverse migrations
- Feature flags for webhook processing enable/disable
- Fallback to existing calendar sync mechanisms

### Success Metrics
- Webhook delivery success rate > 99%
- Calendar sync latency reduction
- Reduced manual sync requests
- Zero webhook-related security incidents

---

## Development Workflow

### Phase Implementation Order
1. **Phase 1**: Core infrastructure (1-2 weeks)
2. **Phase 2**: Google Calendar webhooks (2-3 weeks)
3. **Phase 3**: Microsoft Outlook webhooks (2-3 weeks)
4. **Phase 4**: Management & monitoring (2-3 weeks)
5. **Phase 5**: Advanced features (3-4 weeks)

### Quality Gates
- All unit tests passing
- Integration tests with mock providers
- Security review for webhook validation
- Performance testing under load
- Code review and documentation complete

### Risk Mitigation
- Feature flags for gradual rollout
- Comprehensive logging and monitoring
- Webhook replay capability for debugging
- Circuit breakers for external API calls
- Graceful degradation to manual sync

This plan ensures a systematic approach to implementing calendar webhook functionality while maintaining system stability and enabling incremental testing and deployment.
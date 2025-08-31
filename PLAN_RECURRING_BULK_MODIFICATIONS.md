# Plan: Recurring Object Bulk Modifications (From Nth Occurrence Onwards)

## Overview

This plan outlines the implementation of a feature to modify or cancel recurring objects (events, blocked times, available times) from the Nth occurrence onwards, affecting all subsequent occurrences while leaving previous ones unchanged.

## Current State Analysis

The existing system handles individual occurrence exceptions through:
- **Individual Exception Models**: `EventRecurrenceException`, `BlockedTimeRecurrenceException`, `AvailableTimeRecurrenceException`
- **Generic Exception Handler**: `_create_recurring_exception_generic()`
- **Concrete Methods**: `create_recurring_event_exception()`, `create_recurring_blocked_time_exception()`, `create_recurring_available_time_exception()`
- **Database Functions**: Calculate recurring occurrences with exception handling

## Proposed Solution Architecture

### Core Concept
Instead of creating individual exceptions for each occurrence, we'll:
1. **Truncate the original recurrence rule** by setting its `UNTIL` date to just before the modification date
2. **Create a new recurring object** starting from the modification date with the new rules and modifications
3. **Link them together** for proper tracking and cleanup

### Key Components to Implement

1. **Bulk Exception Models** - New models to track bulk modifications
2. **Recurrence Rule Splitting Logic** - Logic to split recurrence rules at specific occurrences
3. **Generic Bulk Exception Handler** - Generic method to handle bulk modifications
4. **Concrete Bulk Methods** - Specific implementations for each object type
5. **Database Function Updates** - Support for bulk exception handling
6. **Migration and Cleanup** - Proper data migration and cleanup logic

## Implementation Plan

### Phase 1: Core Infrastructure and Models

#### 1.1 Create Bulk Exception Models
Create new models to track when a recurring series has been split:

```python
class RecurrenceBulkModificationMixin(OrganizationModel):
    """Base mixin for tracking bulk modifications to recurring series."""
    
    modification_start_date = models.DateTimeField(
        help_text="The date from which the modification applies"
    )
    original_parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Original recurring object before split"
    )
    modified_continuation = models.ForeignKey(
        'self', 
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="New recurring object for modified series"
    )
    is_bulk_cancelled = models.BooleanField(
        default=False,
        help_text="True if all occurrences from this date are cancelled"
    )
    
class EventBulkModification(RecurrenceBulkModificationMixin):
    parent_event = OrganizationForeignKey(CalendarEvent, ...)
    
class BlockedTimeBulkModification(RecurrenceBulkModificationMixin):
    parent_blocked_time = OrganizationForeignKey(BlockedTime, ...)
    
class AvailableTimeBulkModification(RecurrenceBulkModificationMixin):
    parent_available_time = OrganizationForeignKey(AvailableTime, ...)
```

#### 1.2 Add Bulk Tracking Fields to Recurring Models
Add fields to track bulk modifications:

```python
# Add to CalendarEvent, BlockedTime, AvailableTime models
bulk_modification_parent = OrganizationForeignKey(
    'self',
    on_delete=models.CASCADE,
    null=True,
    blank=True,
    related_name='bulk_modifications',
    help_text="If this is a continuation of a split series"
)
```

**Testing Phase 1:**
- Create and run migrations
- Test model creation and relationships
- Verify organization scoping works correctly

### Phase 2: Recurrence Rule Manipulation

#### 2.1 Recurrence Rule Date-Based Splitting Utilities
Create utilities to split recurrence rules by date:

```python
class RecurrenceRuleSplitter:
    @staticmethod
    def split_at_date(
        original_rule: RecurrenceRule,
        split_date: datetime.datetime,
        original_start: datetime.datetime
    ) -> tuple[RecurrenceRule, RecurrenceRule]:
        """
        Split a recurrence rule into two parts:
        1. Original rule with UNTIL set to just before split_date
        2. New rule starting from split_date
        """
        
    @staticmethod
    def truncate_rule_until_date(
        rule: RecurrenceRule,
        until_date: datetime.datetime
    ) -> RecurrenceRule:
        """
        Truncate a rule by setting its UNTIL date.
        Handles conflicts between COUNT and UNTIL appropriately.
        """
        
    @staticmethod
    def create_continuation_rule(
        original_rule: RecurrenceRule,
        new_start_date: datetime.datetime
    ) -> RecurrenceRule:
        """Create a new rule for the continuation series starting from new_start_date."""
```

#### 2.2 Date-Based Occurrence Validation
Create helpers to validate modification dates:

```python
class OccurrenceValidator:
    @staticmethod
    def validate_modification_date(
        recurring_object: RecurringMixin,
        target_date: datetime.datetime
    ) -> bool:
        """
        Validate that the target date corresponds to a valid occurrence
        of the recurring object.
        """
        
    @staticmethod
    def get_previous_occurrence_date(
        recurring_object: RecurringMixin,
        before_date: datetime.datetime
    ) -> datetime.datetime | None:
        """
        Get the occurrence date immediately before the given date.
        Used to set the UNTIL date for rule truncation.
        """
        
    @staticmethod
    def normalize_modification_date(
        recurring_object: RecurringMixin,
        approximate_date: datetime.datetime
    ) -> datetime.datetime:
        """
        Given an approximate date, find the actual occurrence date
        that's closest to it.
        """
```

**Testing Phase 2:**
- Test recurrence rule splitting with various rule types (daily, weekly, monthly, yearly)
- Test edge cases (rules with existing COUNT/UNTIL, complex BY* rules)
- Verify date-based truncation works correctly across timezones
- Test validation of modification dates against actual occurrences

### Phase 3: Generic Bulk Modification Handler

#### 3.1 Generic Bulk Exception Method
Create a generic method similar to `_create_recurring_exception_generic`:

```python
def _create_recurring_bulk_modification_generic(
    self,
    object_type_name: str,
    parent_object: RecurringMixin,
    modification_start_date: datetime.datetime,
    is_bulk_cancelled: bool = False,
    modification_data: dict[str, Any] | None = None,
    create_continuation_callback: Callable[
        [RecurringMixin, datetime.datetime, RecurrenceRule, dict[str, Any]], RecurringMixin
    ] | None = None,
    bulk_modification_manager_callback: Callable[
        [RecurringMixin, datetime.datetime, RecurringMixin | None], None
    ] | None = None,
) -> RecurringMixin | None:
    """
    Generic method for creating bulk modifications to recurring objects.
    
    This method:
    1. Validates the modification start date is a valid occurrence
    2. Sets the original rule's UNTIL date to just before the modification date
    3. Creates a new recurring object starting from the modification date (if not cancelled)
    4. Records the bulk modification for tracking
    """
```

#### 3.2 Bulk Modification Tracking
Implement tracking and cleanup logic:

```python
class BulkModificationManager:
    @staticmethod
    def record_bulk_modification(
        parent_object: RecurringMixin,
        modification_start_date: datetime.datetime,
        continuation_object: RecurringMixin | None = None,
        is_bulk_cancelled: bool = False
    ) -> None:
        """Record a bulk modification for tracking and cleanup."""
        
    @staticmethod
    def cleanup_bulk_modification_chain(
        root_object: RecurringMixin
    ) -> None:
        """Clean up an entire bulk modification chain."""
```

**Testing Phase 3:**
- Test generic bulk modification with mock callbacks
- Test rule truncation using UNTIL dates
- Test continuation object creation with various modification types
- Test bulk modification tracking and cleanup

### Phase 4: Concrete Bulk Modification Methods

#### 4.1 Event Bulk Modifications
Implement bulk modifications for calendar events:

```python
def create_recurring_event_bulk_modification(
    self,
    parent_event: CalendarEvent,
    modification_start_date: datetime.datetime,
    modified_title: str | None = None,
    modified_description: str | None = None,
    modified_start_time_offset: datetime.timedelta | None = None,
    modified_end_time_offset: datetime.timedelta | None = None,
    is_bulk_cancelled: bool = False,
) -> CalendarEvent | None:
    """Create bulk modification for recurring events from the specified date onwards."""
```

#### 4.2 BlockedTime Bulk Modifications
Implement bulk modifications for blocked times:

```python
def create_recurring_blocked_time_bulk_modification(
    self,
    parent_blocked_time: BlockedTime,
    modification_start_date: datetime.datetime,
    modified_reason: str | None = None,
    modified_start_time_offset: datetime.timedelta | None = None,
    modified_end_time_offset: datetime.timedelta | None = None,
    is_bulk_cancelled: bool = False,
) -> BlockedTime | None:
    """Create bulk modification for recurring blocked times from the specified date onwards."""
```

#### 4.3 AvailableTime Bulk Modifications
Implement bulk modifications for available times:

```python
def create_recurring_available_time_bulk_modification(
    self,
    parent_available_time: AvailableTime,
    modification_start_date: datetime.datetime,
    modified_start_time_offset: datetime.timedelta | None = None,
    modified_end_time_offset: datetime.timedelta | None = None,
    is_bulk_cancelled: bool = False,
) -> AvailableTime | None:
    """Create bulk modification for recurring available times from the specified date onwards."""
```

**Testing Phase 4:**
- Test each concrete implementation independently
- Test with various modification scenarios (time changes, cancellations, property changes)
- Test integration with existing exception system

### Phase 5: Database Function Updates

#### 5.1 Update Occurrence Calculation Functions
Modify existing database functions to handle date-based rule truncation:

```sql
-- Update calculate_recurring_events function
-- Add support for UNTIL-based rule truncation
-- Ensure proper handling of continuation series starting from specific dates

-- Update calculate_recurring_blocked_times function
-- Add date-based bulk modification support

-- Update calculate_recurring_available_times function  
-- Add date-based bulk modification support
```

#### 5.2 Occurrence Query Optimization
Update model managers to efficiently query occurrences with bulk modifications:

```python
class RecurringMixin:
    def get_occurrences_in_range_with_bulk_modifications(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_continuations: bool = True,
        max_occurrences: int = 10000,
    ) -> list[Self]:
        """Get occurrences considering bulk modifications."""
```

**Testing Phase 5:**
- Test database function updates with complex scenarios
- Performance testing with large recurring series
- Test edge cases and boundary conditions

### Phase 6: Integration and API Updates

#### 6.1 Service Layer Integration
Update calendar service to expose bulk modification methods:

```python
class CalendarService:
    def modify_recurring_event_from_date(self, ...):
    def cancel_recurring_event_from_date(self, ...):
    def modify_recurring_blocked_time_from_date(self, ...):
    def cancel_recurring_blocked_time_from_date(self, ...):
    def modify_recurring_available_time_from_date(self, ...):
    def cancel_recurring_available_time_from_date(self, ...):
```

#### 6.2 External Provider Integration
Ensure bulk modifications work with external calendar providers:

```python
class CalendarAdapter:
    def handle_bulk_modification_sync(
        self,
        original_event: CalendarEvent,
        continuation_event: CalendarEvent | None,
        modification_start_date: datetime.datetime
    ) -> None:
        """Handle syncing bulk modifications to external providers."""
```

#### 6.3 API Endpoints
Create new API endpoints for bulk modifications:

```python
# New endpoints:
# POST /api/calendars/{id}/events/{event_id}/bulk-modify/
# POST /api/calendars/{id}/blocked-times/{id}/bulk-modify/
# POST /api/calendars/{id}/available-times/{id}/bulk-modify/
```

**Testing Phase 6:**
- Integration testing with external calendar providers
- End-to-end API testing
- Test synchronization with Google Calendar, Outlook, etc.

## Key Considerations

### Performance
- Database function optimization for bulk operations
- Indexing strategy for new bulk modification tables
- Query optimization for occurrence calculations

### Data Integrity
- Proper cascading deletes for bulk modification chains
- Validation of modification dates against actual occurrences
- Prevention of overlapping bulk modifications
- Proper handling of timezone considerations in date-based truncation

### External Provider Compatibility
- Handling bulk modifications in Google Calendar sync
- Outlook calendar integration considerations
- Graceful degradation when providers don't support bulk operations

### Backward Compatibility
- Ensure existing individual exception system continues to work
- Migration path for existing exceptions
- API versioning considerations

### Edge Cases
- Handling bulk modifications on already modified series
- Complex recurrence rules (multiple BY* clauses)
- Timezone handling across bulk modifications and date-based truncation
- Performance with very large recurring series
- Rules with existing COUNT limitations vs. new UNTIL dates
- Daylight saving time transitions affecting modification dates

## Success Criteria

1. **Functional**: Ability to modify/cancel recurring objects from Nth occurrence onwards
2. **Performance**: No significant performance degradation for existing functionality
3. **Data Integrity**: No data loss during migrations or operations
4. **External Sync**: Proper synchronization with external calendar providers
5. **Backward Compatibility**: Existing functionality remains unchanged
6. **Test Coverage**: Comprehensive test coverage for all new functionality

## Timeline Estimate

- **Phase 1-2**: 1-2 weeks (Models and core utilities)
- **Phase 3**: 1 week (Generic handler)
- **Phase 4**: 1-2 weeks (Concrete implementations)
- **Phase 5**: 1-2 weeks (Database functions)
- **Phase 6**: 1-2 weeks (Integration and APIs)
- **Phase 7**: 1 week (Migration and cleanup)

**Total**: 6-9 weeks with proper testing and validation at each phase.

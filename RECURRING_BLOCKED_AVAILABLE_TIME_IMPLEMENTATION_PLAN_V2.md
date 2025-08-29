# Recurring BlockedTime and AvailableTime Implementation Plan (v2 - Abstract Mixin Approach)

## Overview

This plan outlines the implementation of recurring functionality for `BlockedTime` and `AvailableTime` models using an abstract mixin approach. This version creates a reusable `RecurringMixin` abstract model that can be shared across different models, reducing code duplication and providing a consistent interface.

## Current Implementation Analysis

### Existing Recurring Events Structure

The current recurring events system uses:

1. **RecurrenceRule Model** (`calendar_integration/models.py:270-400`):
   - Stores RFC 5545 compliant recurrence patterns
   - Fields: `frequency`, `interval`, `count`, `until`, `by_weekday`, `by_month_day`, etc.
   - Methods: `to_rrule_string()`, `from_rrule_string()`

2. **CalendarEvent Recurrence Fields** (`calendar_integration/models.py:527-546`):
   - `recurrence_rule`: OneToOne relationship to RecurrenceRule
   - `recurrence_id`: Identifies which occurrence instance this is
   - `parent_event`: Points to master recurring event
   - `is_recurring_exception`: Marks modified occurrences

3. **Database Functions**:
   - `calculate_recurring_events` (`calendar_integration/migrations/sql/functions/calculate_recurring_events/0001.sql`)
   - `get_event_occurrences_json` (`calendar_integration/migrations/sql/functions/get_event_occurrences_json/0001.sql`)

4. **Service Methods** (`calendar_integration/services/calendar_service.py`):
   - `create_event()` (`calendar_integration/services/calendar_service.py:594-741`)
   - `update_event()` (`calendar_integration/services/calendar_service.py:796-1007`)
   - `create_recurring_event()` (`calendar_integration/services/calendar_service.py:1009-1052`)
   - `get_calendar_events_expanded()` (`calendar_integration/services/calendar_service.py:1196-1287`)

## Implementation Plan (Abstract Mixin Approach)

### Phase 1: Create Abstract Recurring Mixin

#### 1.1 Create RecurringMixin Abstract Model

**File**: `calendar_integration/models.py`
**Location**: Add before existing models (around line 25)

Create a generic abstract model that provides recurring functionality:

```python
class RecurringMixin(models.Model):
    """
    Abstract mixin that provides recurring functionality to any model.
    Models that inherit from this mixin must have 'start_time' and 'end_time' fields.
    """
    
    # Recurrence fields
    recurrence_rule = OrganizationOneToOneField(
        RecurrenceRule,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="The recurrence rule for this object. If set, this object is recurring.",
    )
    recurrence_id = models.DateTimeField(
        null=True,
        blank=True,
        help_text="For recurring instances, this identifies which occurrence this is",
    )
    parent_recurring_object = OrganizationForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="recurring_instances",
        help_text="If this is an instance of a recurring object, points to the parent object",
    )
    is_recurring_exception = models.BooleanField(
        default=False,
        help_text="True if this object is an exception to the recurrence rule (modified occurrence)",
    )

    class Meta:
        abstract = True

    @property
    def is_recurring(self) -> bool:
        """Returns True if this object has a recurrence rule."""
        return self.recurrence_rule is not None

    @property
    def is_recurring_instance(self) -> bool:
        """Returns True if this object is an instance of a recurring object."""
        return self.parent_recurring_object is not None

    @property
    def duration(self):
        """Returns the duration of the object as a timedelta."""
        return self.end_time - self.start_time

    def get_occurrences_in_range(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_self=True,
        include_exceptions=True,
        max_occurrences=10000,
    ) -> list["RecurringMixin"]:
        """
        Get occurrences of this recurring object in a date range.
        This method should be overridden by concrete models to provide model-specific logic.
        """
        raise NotImplementedError("Subclasses must implement get_occurrences_in_range")

    def get_generated_occurrences_in_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list["RecurringMixin"]:
        """
        Get generated occurrences using database function.
        This method should be overridden by concrete models to use their specific database function.
        """
        raise NotImplementedError("Subclasses must implement get_generated_occurrences_in_range")

    def create_exception(self, exception_date, is_cancelled=True, modified_object=None):
        """
        Create an exception for a recurring object.
        This method should be overridden by concrete models to provide model-specific logic.
        """
        raise NotImplementedError("Subclasses must implement create_exception")

    def _create_recurring_instance(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        recurrence_id: datetime.datetime,
        is_exception: bool = False,
    ):
        """
        Helper method to create a recurring instance.
        This should be overridden by concrete models to set model-specific fields.
        """
        raise NotImplementedError("Subclasses must implement _create_recurring_instance")
```

#### 1.2 Create Generic Recurring QuerySet and Manager

**File**: `calendar_integration/querysets.py`
**Location**: Add new queryset class after imports

Create a generic queryset that can be inherited by specific model querysets to enable proper method chaining:

```python
class RecurringQuerySetMixin:
    """
    Mixin for querysets that provides recurring functionality.
    Should be used with querysets that inherit from BaseOrganizationModelQuerySet.
    """
    
    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        This method should be overridden by concrete querysets to use their specific database function.
        """
        raise NotImplementedError("Concrete querysets must implement annotate_recurring_occurrences_on_date_range")

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.filter(parent_recurring_object__isnull=True)

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.filter(parent_recurring_object__isnull=False)

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.filter(recurrence_rule__isnull=False)

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.filter(recurrence_rule__isnull=True)
```

**File**: `calendar_integration/managers.py`
**Location**: Add new manager class

Create a generic manager that delegates to queryset methods for proper method chaining:

```python
class RecurringManagerMixin:
    """
    Mixin for managers that provides recurring functionality.
    Should be used with managers that inherit from BaseOrganizationManager.
    The QuerySet should also inherit from RecurringQuerySetMixin.
    """
    
    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        Delegates to the queryset implementation.
        """
        return self.get_queryset().annotate_recurring_occurrences_on_date_range(
            start_date, end_date, max_occurrences
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.get_queryset().filter_master_recurring_objects()

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.get_queryset().filter_recurring_instances()

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.get_queryset().filter_recurring_objects()

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.get_queryset().filter_non_recurring_objects()
```

### Phase 2: Database Functions (Generic Approach)

#### 2.1 Create Generic Database Function Template

**File**: `calendar_integration/migrations/sql/functions/calculate_recurring_objects_base.sql`

Create a base SQL template that can be adapted for different object types:

```sql
-- Base template for calculating recurring object occurrences
-- This template should be copied and adapted for specific object types
-- Parameters:
--   p_table_name: Name of the table (e.g., 'calendar_integration_blockedtime')
--   p_object_id_field: Name of the ID field (e.g., 'id')
--   p_start_time_field: Name of the start time field (e.g., 'start_time')
--   p_end_time_field: Name of the end time field (e.g., 'end_time')
```

#### 2.2 Create Specific Database Functions Using Template

**Directory**: `calendar_integration/migrations/sql/functions/calculate_recurring_blocked_times/`

1. **File**: `calendar_integration/migrations/sql/functions/calculate_recurring_blocked_times/0001.sql`
   - Copy and adapt the base template for blocked times
   - Function signature: `calculate_recurring_blocked_times(p_blocked_time_id, p_start_date, p_end_date, p_max_occurrences)`

2. **File**: `calendar_integration/migrations/sql/functions/get_blocked_time_occurrences_json/0001.sql`
   - Adapt the JSON function for blocked times
   - Function signature: `get_blocked_time_occurrences_json(p_blocked_time_id, p_start_date, p_end_date, p_max_occurrences)`

**Directory**: `calendar_integration/migrations/sql/functions/calculate_recurring_available_times/`

3. **File**: `calendar_integration/migrations/sql/functions/calculate_recurring_available_times/0001.sql`
   - Copy and adapt the base template for available times
   - Function signature: `calculate_recurring_available_times(p_available_time_id, p_start_date, p_end_date, p_max_occurrences)`

4. **File**: `calendar_integration/migrations/sql/functions/get_available_time_occurrences_json/0001.sql`
   - Adapt the JSON function for available times
   - Function signature: `get_available_time_occurrences_json(p_available_time_id, p_start_date, p_end_date, p_max_occurrences)`

#### 2.3 Create Generic Django ORM Functions

**File**: `calendar_integration/database_functions.py`

Add new Django ORM function classes that can be reused:

```python
class GetRecurringOccurrencesJSON(Func):
    """
    Base class for recurring occurrences JSON functions.
    Should be subclassed for specific object types.
    """
    output_field = ArrayField(JSONField())
    
    def __init__(self, object_id_field, start_date, end_date, max_occurrences=1000, **extra):
        super().__init__(object_id_field, start_date, end_date, max_occurrences, **extra)

class GetBlockedTimeOccurrencesJSON(GetRecurringOccurrencesJSON):
    """Django database function to get blocked time occurrences as JSON array."""
    function = "get_blocked_time_occurrences_json"

class GetAvailableTimeOccurrencesJSON(GetRecurringOccurrencesJSON):
    """Django database function to get available time occurrences as JSON array."""
    function = "get_available_time_occurrences_json"
```

### Phase 3: Migrate Existing CalendarEvent Model (Test-First Approach)

#### 3.1 Update CalendarEvent QuerySet and Manager

**File**: `calendar_integration/querysets.py`
**Location**: Update `CalendarEventQuerySet` class

Update `CalendarEventQuerySet` to inherit from `RecurringQuerySetMixin`:

```python
class CalendarEventQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for CalendarEvent model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring event within the specified date range.
        The occurrences are calculated dynamically based on the master event's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetEventOccurrencesJSON("id", start, end, max_occurrences)
        )
```

**File**: `calendar_integration/managers.py`
**Location**: Update `CalendarEventManager` class

Update `CalendarEventManager` to inherit from `RecurringManagerMixin` and properly delegate to queryset:

```python
class CalendarEventManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Custom manager for CalendarEvent model to handle specific queries."""

    def get_queryset(self) -> CalendarEventQuerySet:
        return CalendarEventQuerySet(self.model, using=self._db)

    # Implement RecurringManagerMixin methods by delegating to queryset
    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        return self.get_queryset().annotate_recurring_occurrences_on_date_range(
            start, end, max_occurrences
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.get_queryset().filter_master_recurring_objects()

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.get_queryset().filter_recurring_instances()

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.get_queryset().filter_recurring_objects()

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.get_queryset().filter_non_recurring_objects()
```

#### 3.2 Refactor CalendarEvent Model to use RecurringMixin

**File**: `calendar_integration/models.py`
**Location**: `CalendarEvent` class (`calendar_integration/models.py:450+`)

**⚠️ IMPORTANT: This step should be done carefully to ensure all existing tests pass before proceeding with BlockedTime and AvailableTime implementation.**

Refactor `CalendarEvent` to inherit from both `OrganizationModel` and `RecurringMixin`:

```python
class CalendarEvent(OrganizationModel, RecurringMixin):
    """
    Represents a calendar event.
    """
    # Remove existing recurrence fields (they're now in RecurringMixin):
    # - recurrence_rule (now inherited)
    # - recurrence_id (now inherited) 
    # - parent_event (now parent_recurring_object)
    # - is_recurring_exception (now inherited)
    
    # Keep all other existing fields...
    calendar = OrganizationForeignKey(...)
    title = models.CharField(...)
    description = models.TextField(blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    external_id = models.CharField(max_length=255, unique=True, blank=True)
    # ... etc (all other existing fields remain)
    
    # Override RecurringMixin methods with CalendarEvent-specific logic
    def get_occurrences_in_range(self, start_date, end_date, include_self=True, include_exceptions=True, max_occurrences=10000):
        """CalendarEvent-specific implementation of get_occurrences_in_range."""
        # Move existing CalendarEvent.get_occurrences_in_range logic here
        
    def get_generated_occurrences_in_range(self, start_date, end_date):
        """CalendarEvent-specific implementation using event database function."""
        # Move existing CalendarEvent.get_generated_occurrences_in_range logic here
        
    def create_exception(self, exception_date, is_cancelled=True, modified_event=None):
        """CalendarEvent-specific implementation of create_exception."""
        # Move existing CalendarEvent.create_exception logic here
        
    def _create_recurring_instance(self, start_time, end_time, recurrence_id, is_exception=False):
        """Create a CalendarEvent instance for recurring occurrences."""
        return CalendarEvent(
            calendar_fk=self.calendar_fk,
            organization=self.organization,
            title=self.title,
            description=self.description,
            start_time=start_time,
            end_time=end_time,
            parent_recurring_object_fk=self,
            recurrence_id=recurrence_id,
            is_recurring_exception=is_exception,
            # Copy other relevant fields...
        )
```

#### 3.3 Run Tests and Validate Migration

After completing the CalendarEvent migration:

1. **Run Django migrations**: `python manage.py makemigrations && python manage.py migrate`
2. **Run all tests**: `python manage.py test calendar_integration`
3. **Verify no regressions**: Ensure all existing CalendarEvent functionality works as expected
4. **Test recurring functionality**: Validate that existing recurring events still work properly

**Only proceed to Phase 4 after all tests are passing and CalendarEvent migration is stable.**

### Phase 4: Add BlockedTime and AvailableTime Models

#### 4.1 Update BlockedTime Model

**File**: `calendar_integration/models.py`
**Location**: `BlockedTime` class (`calendar_integration/models.py:824-867`)

Update `BlockedTime` to inherit from `RecurringMixin`:

```python
class BlockedTime(OrganizationModel, RecurringMixin):
    """
    Represents a blocked time period in a calendar.
    """

    calendar = OrganizationForeignKey(...)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    # Bundle calendar fields (keep existing)
    bundle_calendar = OrganizationForeignKey(...)
    bundle_primary_event = OrganizationForeignKey(...)

    # RecurringMixin provides: recurrence_rule, recurrence_id, parent_recurring_object, is_recurring_exception

    def __str__(self):
        return f"Blocked from {self.start_time} to {self.end_time} ({self.reason})"

    @property
    def is_bundle_representation(self) -> bool:
        """Returns True if this blocked time represents a bundle event."""
        return self.bundle_primary_event is not None

    # Implement RecurringMixin abstract methods
    def get_occurrences_in_range(self, start_date, end_date, include_self=True, include_exceptions=True, max_occurrences=10000):
        """BlockedTime-specific implementation of get_occurrences_in_range."""
        # Implementation using blocked time specific logic
        
    def get_generated_occurrences_in_range(self, start_date, end_date):
        """BlockedTime-specific implementation using blocked time database function."""
        # Implementation using GetBlockedTimeOccurrencesJSON
        
    def create_exception(self, exception_date, is_cancelled=True, modified_blocked_time=None):
        """BlockedTime-specific implementation of create_exception."""
        # Implementation for blocked time exceptions
        
    def _create_recurring_instance(self, start_time, end_time, recurrence_id, is_exception=False):
        """Create a BlockedTime instance for recurring occurrences."""
        return BlockedTime(
            calendar_fk=self.calendar_fk,
            organization=self.organization,
            start_time=start_time,
            end_time=end_time,
            reason=self.reason,
            external_id=self.external_id,
            parent_recurring_object_fk=self,
            recurrence_id=recurrence_id,
            is_recurring_exception=is_exception,
        )

    class Meta:
        unique_together = (("calendar_fk_id", "external_id"),)
```

#### 4.2 Update AvailableTime Model

**File**: `calendar_integration/models.py`
**Location**: `AvailableTime` class (`calendar_integration/models.py:870-885`)

Update `AvailableTime` to inherit from `RecurringMixin`:

```python
class AvailableTime(OrganizationModel, RecurringMixin):
    """
    Represents available time slots in a calendar.
    """

    calendar = OrganizationForeignKey(...)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    # RecurringMixin provides: recurrence_rule, recurrence_id, parent_recurring_object, is_recurring_exception

    def __str__(self):
        return f"Available from {self.start_time} to {self.end_time}"

    # Implement RecurringMixin abstract methods
    def get_occurrences_in_range(self, start_date, end_date, include_self=True, include_exceptions=True, max_occurrences=10000):
        """AvailableTime-specific implementation of get_occurrences_in_range."""
        # Implementation using available time specific logic
        
    def get_generated_occurrences_in_range(self, start_date, end_date):
        """AvailableTime-specific implementation using available time database function."""
        # Implementation using GetAvailableTimeOccurrencesJSON
        
    def create_exception(self, exception_date, is_cancelled=True, modified_available_time=None):
        """AvailableTime-specific implementation of create_exception."""
        # Implementation for available time exceptions
        
    def _create_recurring_instance(self, start_time, end_time, recurrence_id, is_exception=False):
        """Create an AvailableTime instance for recurring occurrences."""
        return AvailableTime(
            calendar_fk=self.calendar_fk,
            organization=self.organization,
            start_time=start_time,
            end_time=end_time,
            parent_recurring_object_fk=self,
            recurrence_id=recurrence_id,
            is_recurring_exception=is_exception,
        )
```

### Phase 5: Update Managers and QuerySets

#### 4.1 Create Specific QuerySets Using Mixin

**File**: `calendar_integration/querysets.py`

Create new querysets for BlockedTime and AvailableTime that inherit from the mixin:

```python
class BlockedTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """Custom QuerySet for BlockedTime model to handle specific queries."""
    
    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """Annotate blocked times with their recurring occurrences in the date range."""
        from calendar_integration.database_functions import GetBlockedTimeOccurrencesJSON
        return self.annotate(
            recurring_occurrences=GetBlockedTimeOccurrencesJSON("id", start_date, end_date, max_occurrences)
        )

class AvailableTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """Custom QuerySet for AvailableTime model to handle specific queries."""
    
    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """Annotate available times with their recurring occurrences in the date range."""
        from calendar_integration.database_functions import GetAvailableTimeOccurrencesJSON
        return self.annotate(
            recurring_occurrences=GetAvailableTimeOccurrencesJSON("id", start_date, end_date, max_occurrences)
        )
```

#### 4.2 Create Specific Managers Using Mixin

**File**: `calendar_integration/managers.py`

Update imports to include the new querysets and create new managers:

```python
from calendar_integration.querysets import (
    CalendarEventQuerySet,
    CalendarQuerySet,
    CalendarSyncQuerySet,
    BlockedTimeQuerySet,
    AvailableTimeQuerySet,
)
```

Create new managers that inherit from the mixin (which already provides delegation to querysets):

```python
class BlockedTimeManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Manager for BlockedTime model with recurring support."""
    
    def get_queryset(self) -> BlockedTimeQuerySet:
        return BlockedTimeQuerySet(self.model, using=self._db)
    
    # All recurring methods are inherited from RecurringManagerMixin and delegate to the queryset

class AvailableTimeManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Manager for AvailableTime model with recurring support."""
    
    def get_queryset(self) -> AvailableTimeQuerySet:
        return AvailableTimeQuerySet(self.model, using=self._db)
    
    # All recurring methods are inherited from RecurringManagerMixin and delegate to the queryset
```

#### 4.3 Update Model Manager References

**File**: `calendar_integration/models.py`

Update model manager references:

```python
class CalendarEvent(OrganizationModel, RecurringMixin):
    # ... fields ...
    objects: CalendarEventManager = CalendarEventManager()

class BlockedTime(OrganizationModel, RecurringMixin):
    # ... fields ...
    objects: BlockedTimeManager = BlockedTimeManager()

class AvailableTime(OrganizationModel, RecurringMixin):
    # ... fields ...
    objects: AvailableTimeManager = AvailableTimeManager()
```

### Phase 6: Service Layer with Generic Approach

#### 5.1 Create Generic Recurring Service Methods

**File**: `calendar_integration/services/calendar_service.py`
**Location**: Add base recurring methods

Add generic methods that can work with any recurring object:

```python
class CalendarService(BaseCalendarService):
    
    def _create_recurrence_rule_if_needed(self, rrule_string: str | None) -> RecurrenceRule | None:
        """Helper method to create recurrence rule from RRULE string if provided."""
        if not rrule_string:
            return None
        
        recurrence_rule = RecurrenceRule.from_rrule_string(rrule_string, self.organization)
        recurrence_rule.save()
        return recurrence_rule

    def _get_recurring_objects_expanded(
        self,
        model_class,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        calendar_filter_field: str = "calendar",
    ):
        """Generic method for getting expanded recurring objects."""
        base_qs = (
            model_class.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                parent_recurring_object__isnull=True,  # Master objects only
                organization_id=calendar.organization_id,
            )
        )
        
        # Apply calendar filter
        base_qs = base_qs.filter(**{calendar_filter_field: calendar})

        # Get non-recurring objects within the date range
        non_recurring_objects = base_qs.filter(
            Q(start_time__range=(start_date, end_date)) | Q(end_time__range=(start_date, end_date)),
            recurrence_rule__isnull=True,  # Non-recurring only
        )

        # Get recurring master objects and generate their instances
        recurring_objects = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        objects = list(non_recurring_objects)

        for master_object in recurring_objects:
            instances = master_object.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True
            )
            objects.extend(instances)

        # Sort by start time
        objects.sort(key=lambda x: x.start_time)
        return objects
```

#### 5.2 Update Existing Bulk Service Methods

**File**: `calendar_integration/services/calendar_service.py`

Update the existing bulk methods to support both regular and recurring objects:

```python
class CalendarService(BaseCalendarService):
    
    @transaction.atomic()
    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str, str | None]],
    ) -> Iterable[BlockedTime]:
        """
        Create new blocked times for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the blocked times for.
        :param blocked_times: Iterable of tuples containing (start_time, end_time, reason, rrule_string).
        :return: List of created BlockedTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        blocked_times_to_create = []
        
        for start_time, end_time, reason, rrule_string in blocked_times:
            # Create recurrence rule if provided
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
            
            blocked_time = BlockedTime(
                calendar=calendar,
                start_time=start_time,
                end_time=end_time,
                reason=reason,
                organization_id=calendar.organization_id,
                recurrence_rule_fk=recurrence_rule,
            )
            blocked_times_to_create.append(blocked_time)

        return BlockedTime.objects.bulk_create(blocked_times_to_create)

    @transaction.atomic()
    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[tuple[datetime.datetime, datetime.datetime, str | None]],
    ) -> Iterable[AvailableTime]:
        """
        Create availability windows for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the availability windows for.
        :param availability_windows: Iterable of tuples containing (start_time, end_time, rrule_string).
        :return: List of created AvailableTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not calendar.manage_available_windows:
            raise ValueError("This calendar does not manage available windows.")

        availability_windows_to_create = []
        
        for start_time, end_time, rrule_string in availability_windows:
            # Create recurrence rule if provided
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
            
            available_time = AvailableTime(
                calendar=calendar,
                start_time=start_time,
                end_time=end_time,
                organization_id=calendar.organization_id,
                recurrence_rule_fk=recurrence_rule,
            )
            availability_windows_to_create.append(available_time)

        return AvailableTime.objects.bulk_create(availability_windows_to_create)

    # Convenience methods for single object creation
    @transaction.atomic()
    def create_blocked_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        reason: str = "",
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Create a single blocked time (optionally recurring)."""
        result = self.bulk_create_manual_blocked_times(
            calendar=calendar,
            blocked_times=[(start_time, end_time, reason, rrule_string)]
        )
        return list(result)[0]

    @transaction.atomic()
    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        rrule_string: str | None = None,
    ) -> AvailableTime:
        """Create a single available time (optionally recurring)."""
        result = self.bulk_create_availability_windows(
            calendar=calendar,
            availability_windows=[(start_time, end_time, rrule_string)]
        )
        return list(result)[0]
```

#### 5.3 Add Expanded Retrieval Methods

**File**: `calendar_integration/services/calendar_service.py`

Add methods for getting expanded recurring objects:

```python
class CalendarService(BaseCalendarService):

    def get_blocked_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        """Get all blocked times in a date range with recurring blocked times expanded to instances."""
        return self._get_recurring_objects_expanded(
            BlockedTime, calendar, start_date, end_date
        )

    def get_available_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        """Get all available times in a date range with recurring available times expanded to instances."""
        return self._get_recurring_objects_expanded(
            AvailableTime, calendar, start_date, end_date
        )
```

### Phase 7: Input Data Classes (Simplified)

#### 6.1 Create Generic Input Data Classes

**File**: `calendar_integration/services/dataclasses.py`

Create base classes and specific implementations:

```python
@dataclass
class RecurringObjectInputData:
    """Base class for recurring object input data."""
    start_time: datetime.datetime
    end_time: datetime.datetime
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False

@dataclass
class BlockedTimeInputData(RecurringObjectInputData):
    """Input data for creating blocked times."""
    calendar_id: int
    reason: str = ""
    external_id: str = ""

@dataclass
class AvailableTimeInputData(RecurringObjectInputData):
    """Input data for creating available times."""
    calendar_id: int
```

### Phase 8: Update Existing Methods

#### 7.1 Update get_unavailable_time_windows_in_range

**File**: `calendar_integration/services/calendar_service.py`
**Location**: `get_unavailable_time_windows_in_range` method

```python
def get_unavailable_time_windows_in_range(
    self,
    calendar: Calendar,
    start_datetime: datetime.datetime,
    end_datetime: datetime.datetime,
) -> list[UnavailableTimeWindow]:
    # ... existing event logic ...
    
    # Replace the current blocked_times query with:
    blocked_times = self.get_blocked_times_expanded(
        calendar=calendar,
        start_date=start_datetime,
        end_date=end_datetime,
    )
    
    # ... rest of the method remains the same ...
```

#### 7.2 Update get_availability_windows_in_range

**File**: `calendar_integration/services/calendar_service.py`
**Location**: `get_availability_windows_in_range` method

```python
def get_availability_windows_in_range(
    self, calendar: Calendar, start_datetime: datetime.datetime, end_datetime: datetime.datetime
) -> Iterable[AvailableTimeWindow]:
    if calendar.manage_available_windows:
        # Replace the current query with:
        available_times = self.get_available_times_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )
        
        return [
            AvailableTimeWindow(
                start_time=available_time.start_time,
                end_time=available_time.end_time,
                id=available_time.id,
                can_book_partially=False,
            )
            for available_time in available_times
        ]
    
    # ... rest of the method remains the same ...
```

### Phase 9: Migration Strategy

#### 9.1 Database Migration Order

**⚠️ IMPORTANT: Follow this exact order to ensure test stability and minimize downtime:**

1. **Migration 1**: Create `RecurringMixin` abstract model (no DB changes, just code structure)
2. **Migration 2**: Update `CalendarEvent` model to inherit from `RecurringMixin` (Phase 3 - Test-First Approach)
   - Update querysets and managers first
   - Migrate model inheritance
   - Run full test suite to ensure no regressions
   - **Only proceed after all tests pass**
3. **Migration 3**: Add recurrence fields to `BlockedTime` and `AvailableTime` models (Phase 4)
4. **Migration 4**: Create `calculate_recurring_blocked_times` database function
5. **Migration 5**: Create `get_blocked_time_occurrences_json` database function
6. **Migration 6**: Create `calculate_recurring_available_times` database function
7. **Migration 7**: Create `get_available_time_occurrences_json` database function

#### 9.2 Data Migration (if needed)

If we need to rename the `CalendarEvent.parent_event` field to `parent_recurring_object`:

**File**: `calendar_integration/migrations/XXXX_rename_parent_event_field.py`

```python
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ('calendar_integration', 'XXXX_add_recurring_mixin'),
    ]

    operations = [
        migrations.RenameField(
            model_name='calendarevent',
            old_name='parent_event',
            new_name='parent_recurring_object',
        ),
    ]
```

### Phase 10: Testing (Enhanced)

#### 9.1 Test Generic Recurring Functionality

**File**: `calendar_integration/tests/test_models.py`

Create tests for the abstract mixin using pytest:

```python
@pytest.mark.django_db
class TestRecurringMixin:
    def test_recurring_mixin_properties(self):
        """Test that recurring mixin properties work correctly."""
    
    def test_recurring_mixin_duration(self):
        """Test duration calculation in mixin."""
    
    def test_recurring_mixin_abstract_methods(self):
        """Test that abstract methods must be implemented by subclasses."""
```

#### 9.2 Test Specific Model Implementations

**File**: `calendar_integration/tests/test_models.py`

Test specific implementations of the mixins.

#### 9.3 Test Generic Service Methods

**File**: `calendar_integration/tests/services/test_calendar_service.py`

Test the generic service methods work with different model types.

#### 9.4 Add more tests to the availability/unavailability methods of the calendar service

**File**: `calendar_integration/tests/services/test_calendar_service.py`

Add more tests to the availability/unavailability methods of the calendar service to cover recurring BlockedTimes and AvailableTimes

### Phase 11: REST API Endpoints

#### 10.1 Create Serializers for BlockedTime and AvailableTime

**File**: `calendar_integration/serializers.py`
**Location**: Add after existing serializers

Create comprehensive serializers for both models with recurring support:

```python
class RecurringBlockedTimeSerializer(serializers.Serializer):
    """Serializer for creating recurring blocked time."""
    
    calendar_id = serializers.IntegerField(required=True, help_text="ID of the calendar")
    start_time = serializers.DateTimeField(required=True, help_text="Start time for the first occurrence")
    end_time = serializers.DateTimeField(required=True, help_text="End time for the first occurrence")
    reason = serializers.CharField(max_length=255, required=False, default="", help_text="Reason for blocking")
    external_id = serializers.CharField(max_length=255, required=False, default="", help_text="External ID")
    recurrence_rule = serializers.CharField(
        required=True, 
        help_text="RRULE string defining the recurrence pattern"
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate_calendar_id(self, calendar_id):
        """Validate calendar exists and user has access."""
        user = self.context["request"].user
        try:
            calendar = Calendar.objects.filter_by_organization(
                user.organization_membership.organization_id
            ).get(id=calendar_id)
            return calendar_id
        except Calendar.DoesNotExist:
            raise ValidationError("Calendar not found or access denied")

    def validate(self, attrs):
        """Validate blocked time data."""
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")
        
        # Validate RRULE string
        try:
            from calendar_integration.models import RecurrenceRule
            RecurrenceRule.from_rrule_string(
                attrs["recurrence_rule"], 
                self.context["request"].user.organization_membership.organization
            )
        except Exception as e:
            raise ValidationError(f"Invalid recurrence rule: {str(e)}")
        
        return attrs

    def save(self, **kwargs):
        """Create recurring blocked time."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        calendar = Calendar.objects.filter_by_organization(organization.id).get(
            id=self.validated_data["calendar_id"]
        )
        
        self.calendar_service.initialize_without_provider(organization)
        
        self.instance = self.calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=self.validated_data["start_time"],
            end_time=self.validated_data["end_time"],
            reason=self.validated_data["reason"],
            external_id=self.validated_data["external_id"],
            rrule_string=self.validated_data["recurrence_rule"],
        )
        return self.instance


class RecurringAvailableTimeSerializer(serializers.Serializer):
    """Serializer for creating recurring available time."""
    
    calendar_id = serializers.IntegerField(required=True, help_text="ID of the calendar")
    start_time = serializers.DateTimeField(required=True, help_text="Start time for the first occurrence")
    end_time = serializers.DateTimeField(required=True, help_text="End time for the first occurrence")
    recurrence_rule = serializers.CharField(
        required=True, 
        help_text="RRULE string defining the recurrence pattern"
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate_calendar_id(self, calendar_id):
        """Validate calendar exists and user has access."""
        user = self.context["request"].user
        try:
            calendar = Calendar.objects.filter_by_organization(
                user.organization_membership.organization_id
            ).get(id=calendar_id)
            return calendar_id
        except Calendar.DoesNotExist:
            raise ValidationError("Calendar not found or access denied")

    def validate(self, attrs):
        """Validate available time data."""
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")
        
        # Validate RRULE string
        try:
            from calendar_integration.models import RecurrenceRule
            RecurrenceRule.from_rrule_string(
                attrs["recurrence_rule"], 
                self.context["request"].user.organization_membership.organization
            )
        except Exception as e:
            raise ValidationError(f"Invalid recurrence rule: {str(e)}")
        
        return attrs

    def save(self, **kwargs):
        """Create recurring available time."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        calendar = Calendar.objects.filter_by_organization(organization.id).get(
            id=self.validated_data["calendar_id"]
        )
        
        self.calendar_service.initialize_without_provider(organization)
        
        self.instance = self.calendar_service.create_available_time(
            calendar=calendar,
            start_time=self.validated_data["start_time"],
            end_time=self.validated_data["end_time"],
            rrule_string=self.validated_data["recurrence_rule"],
        )
        return self.instance


class BlockedTimeSerializer(VirtualModelSerializer):
    """Serializer for BlockedTime model with recurring support."""
    
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for creating recurring blocked times",
    )
    rrule_string = serializers.CharField(
        write_only=True, 
        required=False, 
        help_text="RRULE string for creating recurring blocked times"
    )
    parent_blocked_time_id = serializers.IntegerField(
        write_only=True, 
        required=False, 
        help_text="ID of parent blocked time for recurring instances"
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, 
        help_text="True if this is an instance of a recurring blocked time"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, 
        help_text="True if this is a recurring blocked time"
    )
    parent_blocked_time = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = BlockedTime
        virtual_model = BlockedTimeVirtualModel
        fields = (
            "id",
            "calendar",
            "start_time",
            "end_time",
            "reason",
            "external_id",
            "bundle_calendar",
            "bundle_primary_event",
            "recurrence_rule",
            "rrule_string",
            "parent_blocked_time_id",
            "parent_blocked_time",
            "is_recurring_instance",
            "is_recurring",
            "is_recurring_exception",
            "recurrence_id",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            # it isn't possible to manually create a blocked time with an `external_id`
            "external_id", 
            "is_recurring_instance",
            "parent_blocked_time",
            "bundle_calendar",
            "bundle_primary_event",
            "created",
            "modified",
        )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate(self, attrs):
        """Validate blocked time data."""
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")

        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")
        parent_blocked_time_id = attrs.get("parent_blocked_time_id")

        if recurrence_rule_data and rrule_string:
            raise ValidationError("Cannot provide both recurrence_rule and rrule_string")

        if (recurrence_rule_data or rrule_string) and parent_blocked_time_id:
            raise ValidationError("Cannot set recurrence rule for child instances")

        return attrs

    def create(self, validated_data):
        """Create blocked time using calendar service."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        
        self.calendar_service.initialize_without_provider(organization)
        
        # Extract recurrence rule string
        rrule_string = validated_data.pop("rrule_string", None)
        if validated_data.get("recurrence_rule"):
            # Convert recurrence_rule data to rrule_string if needed
            recurrence_rule_data = validated_data.pop("recurrence_rule")
            # Create temporary RecurrenceRule to get rrule_string
            temp_rule = RecurrenceRule(**recurrence_rule_data, organization=organization)
            rrule_string = temp_rule.to_rrule_string()
        
        return self.calendar_service.create_blocked_time(
            calendar=validated_data["calendar"],
            start_time=validated_data["start_time"],
            end_time=validated_data["end_time"],
            reason=validated_data.get("reason", ""),
            rrule_string=rrule_string,
        )

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: BlockedTime) -> bool:
        """Check if blocked time is recurring."""
        return obj.is_recurring

    @v.hints.no_deferred_fields()
    def get_is_recurring_instance(self, obj: BlockedTime) -> bool:
        """Check if blocked time is a recurring instance."""
        return obj.is_recurring_instance

    @v.hints.no_deferred_fields()
    def get_parent_blocked_time(self, obj: BlockedTime):
        """Get parent blocked time for instances."""
        if obj.parent_recurring_object:
            return {
                "id": obj.parent_recurring_object.id,
                "start_time": obj.parent_recurring_object.start_time,
                "end_time": obj.parent_recurring_object.end_time,
                "reason": obj.parent_recurring_object.reason,
            }
        return None


class AvailableTimeSerializer(VirtualModelSerializer):
    """Serializer for AvailableTime model with recurring support."""
    
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for creating recurring available times",
    )
    rrule_string = serializers.CharField(
        write_only=True, 
        required=False, 
        help_text="RRULE string for creating recurring available times"
    )
    parent_available_time_id = serializers.IntegerField(
        write_only=True, 
        required=False, 
        help_text="ID of parent available time for recurring instances"
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, 
        help_text="True if this is an instance of a recurring available time"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, 
        help_text="True if this is a recurring available time"
    )
    parent_available_time = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AvailableTime
        virtual_model = AvailableTimeVirtualModel
        fields = (
            "id",
            "calendar",
            "start_time",
            "end_time",
            "recurrence_rule",
            "rrule_string",
            "parent_available_time_id",
            "parent_available_time",
            "is_recurring_instance",
            "is_recurring",
            "is_recurring_exception",
            "recurrence_id",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "is_recurring_instance",
            "parent_available_time",
            "created",
            "modified",
        )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate(self, attrs):
        """Validate available time data."""
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")

        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")
        parent_available_time_id = attrs.get("parent_available_time_id")

        if recurrence_rule_data and rrule_string:
            raise ValidationError("Cannot provide both recurrence_rule and rrule_string")

        if (recurrence_rule_data or rrule_string) and parent_available_time_id:
            raise ValidationError("Cannot set recurrence rule for child instances")

        return attrs

    def create(self, validated_data):
        """Create available time using calendar service."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        
        self.calendar_service.initialize_without_provider(organization)
        
        # Extract recurrence rule string
        rrule_string = validated_data.pop("rrule_string", None)
        if validated_data.get("recurrence_rule"):
            # Convert recurrence_rule data to rrule_string if needed
            recurrence_rule_data = validated_data.pop("recurrence_rule")
            # Create temporary RecurrenceRule to get rrule_string
            temp_rule = RecurrenceRule(**recurrence_rule_data, organization=organization)
            rrule_string = temp_rule.to_rrule_string()
        
        return self.calendar_service.create_available_time(
            calendar=validated_data["calendar"],
            start_time=validated_data["start_time"],
            end_time=validated_data["end_time"],
            rrule_string=rrule_string,
        )

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: AvailableTime) -> bool:
        """Check if available time is recurring."""
        return obj.is_recurring

    @v.hints.no_deferred_fields()
    def get_is_recurring_instance(self, obj: AvailableTime) -> bool:
        """Check if available time is a recurring instance."""
        return obj.is_recurring_instance

    @v.hints.no_deferred_fields()
    def get_parent_available_time(self, obj: AvailableTime):
        """Get parent available time for instances."""
        if obj.parent_recurring_object:
            return {
                "id": obj.parent_recurring_object.id,
                "start_time": obj.parent_recurring_object.start_time,
                "end_time": obj.parent_recurring_object.end_time,
            }
        return None


class BulkBlockedTimeInputSerializer(serializers.Serializer):
    """Input serializer for a single blocked time in bulk operations."""
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    reason = serializers.CharField(max_length=255, required=False, default="")
    rrule_string = serializers.CharField(required=False, allow_null=True, help_text="RRULE string for recurring blocked times")

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")
        return attrs


class BulkBlockedTimeSerializer(serializers.Serializer):
    """Serializer for creating multiple blocked times."""
    
    calendar_id = serializers.IntegerField(required=True, help_text="ID of the calendar")
    blocked_times = BulkBlockedTimeInputSerializer(many=True)

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate_calendar_id(self, calendar_id):
        """Validate calendar exists and user has access."""
        user = self.context["request"].user
        try:
            calendar = Calendar.objects.filter_by_organization(
                user.organization_membership.organization_id
            ).get(id=calendar_id)
            return calendar_id
        except Calendar.DoesNotExist:
            raise ValidationError("Calendar not found or access denied")

    def validate_blocked_times(self, blocked_times_data):
        """Validate bulk blocked times data."""
        if not blocked_times_data:
            raise ValidationError("At least one blocked time is required")
        
        if len(blocked_times_data) > 100:  # Reasonable limit
            raise ValidationError("Cannot create more than 100 blocked times at once")
        
        return blocked_times_data

    def save(self, **kwargs):
        """Create multiple blocked times using calendar service."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        calendar = Calendar.objects.filter_by_organization(organization.id).get(
            id=self.validated_data["calendar_id"]
        )
        
        self.calendar_service.initialize_without_provider(organization)
        
        # Convert to the format expected by the service method
        blocked_times_data = [
            (
                bt["start_time"],
                bt["end_time"],
                bt.get("reason", ""),
                bt.get("rrule_string")
            )
            for bt in self.validated_data["blocked_times"]
        ]
        
        blocked_times = self.calendar_service.bulk_create_manual_blocked_times(
            calendar=calendar,
            blocked_times=blocked_times_data
        )
        
        return list(blocked_times)


class BulkAvailableTimeInputSerializer(serializers.Serializer):
    """Input serializer for a single available time in bulk operations."""
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    rrule_string = serializers.CharField(required=False, allow_null=True, help_text="RRULE string for recurring available times")

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise ValidationError("start_time must be before end_time")
        return attrs


class BulkAvailableTimeSerializer(serializers.Serializer):
    """Serializer for creating multiple available times."""
    
    calendar_id = serializers.IntegerField(required=True, help_text="ID of the calendar")
    available_times = BulkAvailableTimeInputSerializer(many=True)

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

    def validate_calendar_id(self, calendar_id):
        """Validate calendar exists and user has access."""
        user = self.context["request"].user
        try:
            calendar = Calendar.objects.filter_by_organization(
                user.organization_membership.organization_id
            ).get(id=calendar_id)
            return calendar_id
        except Calendar.DoesNotExist:
            raise ValidationError("Calendar not found or access denied")

    def validate_available_times(self, available_times_data):
        """Validate bulk available times data."""
        if not available_times_data:
            raise ValidationError("At least one available time is required")
        
        if len(available_times_data) > 100:  # Reasonable limit
            raise ValidationError("Cannot create more than 100 available times at once")
        
        return available_times_data

    def save(self, **kwargs):
        """Create multiple available times using calendar service."""
        if not self.calendar_service:
            raise ValueError("Calendar service not available")
        
        user = self.context["request"].user
        organization = user.organization_membership.organization
        calendar = Calendar.objects.filter_by_organization(organization.id).get(
            id=self.validated_data["calendar_id"]
        )
        
        self.calendar_service.initialize_without_provider(organization)
        
        # Convert to the format expected by the service method
        available_times_data = [
            (
                at["start_time"],
                at["end_time"],
                at.get("rrule_string")
            )
            for at in self.validated_data["available_times"]
        ]
        
        available_times = self.calendar_service.bulk_create_availability_windows(
            calendar=calendar,
            availability_windows=available_times_data
        )
        
        return list(available_times)
```

#### 10.2 Create ViewSets for BlockedTime and AvailableTime

**File**: `calendar_integration/views.py`
**Location**: Add after existing viewsets

Create comprehensive viewsets with all CRUD operations and special actions:

```python
class BlockedTimeViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing blocked times with recurring support.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = BlockedTime.objects.all()
    serializer_class = BlockedTimeSerializer
    filterset_fields = ["calendar", "start_time", "end_time", "reason"]

    def get_queryset(self):
        """Filter blocked times by user's accessible calendar organizations."""
        user = self.request.user
        if not user.is_authenticated:
            return BlockedTime.objects.none()

        try:
            organization_id = user.organization_membership.organization_id
            return super().get_queryset().filter_by_organization(organization_id)
        except OrganizationMembership.DoesNotExist:
            return BlockedTime.objects.none()

    @extend_schema(
        summary="Create recurring blocked time",
        description="Create a recurring blocked time with specified recurrence pattern.",
        request=RecurringBlockedTimeSerializer,
        responses={201: BlockedTimeSerializer},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="create-recurring",
        url_name="create-recurring",
    )
    def create_recurring(self, request):
        """Create a recurring blocked time."""
        serializer = RecurringBlockedTimeSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        blocked_time = serializer.save()

        return Response(
            BlockedTimeSerializer(blocked_time, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Create bulk blocked times",
        description="Create multiple blocked times at once.",
        request=BulkBlockedTimeSerializer,
        responses={201: BlockedTimeSerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="bulk-create",
        url_name="bulk-create",
    )
    def bulk_create(self, request):
        """Create multiple blocked times."""
        serializer = BulkBlockedTimeSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        blocked_times = serializer.save()

        return Response(
            BlockedTimeSerializer(blocked_times, many=True, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Get expanded blocked times",
        description="Get blocked times with recurring instances expanded for a date range.",
        parameters=[
            OpenApiParameter(
                name="calendar_id",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Calendar ID to filter blocked times",
                required=True,
            ),
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start datetime in ISO format",
                required=True,
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End datetime in ISO format",
                required=True,
            ),
        ],
        responses={200: BlockedTimeSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=False,
        url_path="expanded",
        url_name="expanded",
    )
    @inject
    def expanded(
        self,
        request,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """Get expanded blocked times including recurring instances."""
        calendar_id = request.query_params.get("calendar_id")
        start_datetime_str = request.query_params.get("start_datetime")
        end_datetime_str = request.query_params.get("end_datetime")

        if not all([calendar_id, start_datetime_str, end_datetime_str]):
            raise ValidationError({
                "non_field_errors": ["calendar_id, start_datetime, and end_datetime are required"]
            })

        try:
            calendar = Calendar.objects.filter_by_organization(
                request.user.organization_membership.organization_id
            ).get(id=calendar_id)
        except Calendar.DoesNotExist:
            raise ValidationError({"calendar_id": ["Calendar not found or access denied"]})

        try:
            start_datetime = datetime.datetime.fromisoformat(
                start_datetime_str.replace("Z", "+00:00")
            )
            end_datetime = datetime.datetime.fromisoformat(
                end_datetime_str.replace("Z", "+00:00")
            )
        except ValueError as e:
            raise ValidationError({
                "non_field_errors": ["Invalid datetime format. Use ISO format"]
            }) from e

        organization = request.user.organization_membership.organization
        calendar_service.initialize_without_provider(organization)

        blocked_times = calendar_service.get_blocked_times_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        serializer = BlockedTimeSerializer(
            blocked_times, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)


class AvailableTimeViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing available times with recurring support.
    """

    permission_classes = (CalendarAvailabilityPermission,)
    queryset = AvailableTime.objects.all()
    serializer_class = AvailableTimeSerializer
    filterset_fields = ["calendar", "start_time", "end_time"]

    def get_queryset(self):
        """Filter available times by user's accessible calendar organizations."""
        user = self.request.user
        if not user.is_authenticated:
            return AvailableTime.objects.none()

        try:
            organization_id = user.organization_membership.organization_id
            return super().get_queryset().filter_by_organization(organization_id)
        except OrganizationMembership.DoesNotExist:
            return AvailableTime.objects.none()

    @extend_schema(
        summary="Create recurring available time",
        description="Create a recurring available time with specified recurrence pattern.",
        request=RecurringAvailableTimeSerializer,
        responses={201: AvailableTimeSerializer},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="create-recurring",
        url_name="create-recurring",
    )
    def create_recurring(self, request):
        """Create a recurring available time."""
        serializer = RecurringAvailableTimeSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        available_time = serializer.save()

        return Response(
            AvailableTimeSerializer(available_time, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Create bulk available times",
        description="Create multiple available times at once.",
        request=BulkAvailableTimeSerializer,
        responses={201: AvailableTimeSerializer(many=True)},
    )
    @action(
        methods=["POST"],
        detail=False,
        url_path="bulk-create",
        url_name="bulk-create",
    )
    def bulk_create(self, request):
        """Create multiple available times."""
        serializer = BulkAvailableTimeSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        available_times = serializer.save()

        return Response(
            AvailableTimeSerializer(available_times, many=True, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Get expanded available times",
        description="Get available times with recurring instances expanded for a date range.",
        parameters=[
            OpenApiParameter(
                name="calendar_id",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Calendar ID to filter available times",
                required=True,
            ),
            OpenApiParameter(
                name="start_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start datetime in ISO format",
                required=True,
            ),
            OpenApiParameter(
                name="end_datetime",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End datetime in ISO format",
                required=True,
            ),
        ],
        responses={200: AvailableTimeSerializer(many=True)},
    )
    @action(
        methods=["GET"],
        detail=False,
        url_path="expanded",
        url_name="expanded",
    )
    @inject
    def expanded(
        self,
        request,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ):
        """Get expanded available times including recurring instances."""
        calendar_id = request.query_params.get("calendar_id")
        start_datetime_str = request.query_params.get("start_datetime")
        end_datetime_str = request.query_params.get("end_datetime")

        if not all([calendar_id, start_datetime_str, end_datetime_str]):
            raise ValidationError({
                "non_field_errors": ["calendar_id, start_datetime, and end_datetime are required"]
            })

        try:
            calendar = Calendar.objects.filter_by_organization(
                request.user.organization_membership.organization_id
            ).get(id=calendar_id)
        except Calendar.DoesNotExist:
            raise ValidationError({"calendar_id": ["Calendar not found or access denied"]})

        try:
            start_datetime = datetime.datetime.fromisoformat(
                start_datetime_str.replace("Z", "+00:00")
            )
            end_datetime = datetime.datetime.fromisoformat(
                end_datetime_str.replace("Z", "+00:00")
            )
        except ValueError as e:
            raise ValidationError({
                "non_field_errors": ["Invalid datetime format. Use ISO format"]
            }) from e

        organization = request.user.organization_membership.organization
        calendar_service.initialize_without_provider(organization)

        available_times = calendar_service.get_available_times_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        serializer = AvailableTimeSerializer(
            available_times, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)
```

#### 10.3 Add URL Routes

**File**: `calendar_integration/routes.py`
**Location**: Add after existing routes

Add URL patterns for the new viewsets:

```python
from calendar_integration.views import BlockedTimeViewSet, AvailableTimeViewSet

# Add to existing router registration
router.register(r"blocked-times", BlockedTimeViewSet, basename="blocked-time")
router.register(r"available-times", AvailableTimeViewSet, basename="available-time")
```

#### 10.4 Create Virtual Models

**File**: `calendar_integration/virtual_models.py`
**Location**: Add after existing virtual models

Create virtual models for optimized queries:

```python
class BlockedTimeVirtualModel(v.VirtualModel):
    class Meta:
        model = "calendar_integration.BlockedTime"


class AvailableTimeVirtualModel(v.VirtualModel):
    class Meta:
        model = "calendar_integration.AvailableTime"
```

#### 10.5 Add Permissions

**File**: `calendar_integration/permissions.py`
**Location**: Add after existing permissions

Create or update permissions for blocked times and available times:

```python
class BlockedTimePermission(BasePermission):
    """Permission for blocked time operations."""
    
    def has_permission(self, request, view):
        """Check if user has permission to access blocked times."""
        return request.user.is_authenticated and hasattr(request.user, 'organization_membership')
    
    def has_object_permission(self, request, view, obj):
        """Check if user has permission to access specific blocked time."""
        try:
            user_org_id = request.user.organization_membership.organization_id
            return obj.organization_id == user_org_id
        except AttributeError:
            return False


class AvailableTimePermission(BasePermission):
    """Permission for available time operations."""
    
    def has_permission(self, request, view):
        """Check if user has permission to access available times."""
        return request.user.is_authenticated and hasattr(request.user, 'organization_membership')
    
    def has_object_permission(self, request, view, obj):
        """Check if user has permission to access specific available time."""
        try:
            user_org_id = request.user.organization_membership.organization_id
            return obj.organization_id == user_org_id
        except AttributeError:
            return False
```

### Phase 12: API Testing

#### 11.1 Create API Tests for BlockedTime

**File**: `calendar_integration/tests/test_blocked_time_api.py`

Create comprehensive API tests:

```python
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

class BlockedTimeAPITestCase(TestCase):
    def setUp(self):
        """Set up test data."""
        # Create test organization, user, calendar, etc.
        
    def test_create_blocked_time(self):
        """Test creating a single blocked time."""
        
    def test_create_recurring_blocked_time(self):
        """Test creating a recurring blocked time."""
        
    def test_bulk_create_blocked_times(self):
        """Test creating multiple blocked times."""
        
    def test_get_expanded_blocked_times(self):
        """Test getting expanded blocked times with recurring instances."""
        
    def test_update_blocked_time(self):
        """Test updating a blocked time."""
        
    def test_delete_blocked_time(self):
        """Test deleting a blocked time."""
        
    def test_permissions(self):
        """Test that users can only access their organization's blocked times."""
```

#### 11.2 Create API Tests for AvailableTime

**File**: `calendar_integration/tests/test_available_time_api.py`

Create similar comprehensive API tests for available times.

### Phase 13: API Documentation

#### 12.1 Update OpenAPI Schema

Ensure all new endpoints are properly documented with `drf-spectacular` decorators and include:

- Request/response examples
- Parameter descriptions
- Error response codes
- Authentication requirements

#### 12.2 API Usage Examples

**File**: `calendar_integration/docs/api_examples.md`

Create documentation with example API calls:

```markdown
## Blocked Time API Examples

### Create a simple blocked time
```bash
POST /api/blocked-times/
{
    "calendar": 1,
    "start_time": "2025-09-01T09:00:00Z",
    "end_time": "2025-09-01T17:00:00Z",
    "reason": "Office maintenance"
}
```

### Create a recurring blocked time
```bash
POST /api/blocked-times/create-recurring/
{
    "calendar_id": 1,
    "start_time": "2025-09-01T09:00:00Z",
    "end_time": "2025-09-01T17:00:00Z",
    "reason": "Weekly maintenance",
    "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO;COUNT=10"
}
```

### Get expanded blocked times for a date range
```bash
GET /api/blocked-times/expanded/?calendar_id=1&start_datetime=2025-09-01T00:00:00Z&end_datetime=2025-09-30T23:59:59Z
```

## Available Time API Examples

### Create a simple available time
```bash
POST /api/available-times/
{
    "calendar": 1,
    "start_time": "2025-09-01T09:00:00Z",
    "end_time": "2025-09-01T17:00:00Z"
}
```

### Create a recurring available time
```bash
POST /api/available-times/create-recurring/
{
    "calendar_id": 1,
    "start_time": "2025-09-01T09:00:00Z",
    "end_time": "2025-09-01T17:00:00Z",
    "recurrence_rule": "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=50"
}
```

## Summary

This abstract mixin approach provides a much more maintainable and extensible solution for implementing recurring functionality across multiple models. It follows Django best practices for abstract models and provides a clean separation of concerns while maintaining backward compatibility with existing code.

The key improvement is that recurring logic is implemented once in the `RecurringMixin` and then specialized by each concrete model, rather than duplicating similar code across multiple models.

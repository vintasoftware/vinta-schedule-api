# Copilot Contribution Instructions

Welcome to the Vinta Schedule project! This guide provides instructions for contributing to the codebase, with details on architecture, patterns, and best practices.

## Dependency Injection
- We use [`dependency_injector`](https://python-dependency-injector.ets-labs.org/) for dependency injection.
- The `di_core` app contains our DI containers and configuration. All services and components should be registered in the container defined in `di_core/containers.py`.
- When developing new services, inject dependencies via the container rather than direct imports.

## Service Development
- Services are typically placed in the `services/` subdirectory of each app (e.g., `calendar_integration/services/`).
- Services should be stateless and receive dependencies via DI.
- Follow the single responsibility principle: each service should do one thing well.

## Custom Managers and Querysets
- Custom model managers and querysets are defined in each app's `managers.py` and `querysets.py` files.
- Use custom managers to encapsulate query logic and expose domain-specific methods.
- Querysets should be chainable and composable.
- No complex queryset should be created within services/views/serializers. We must always define methods in custom managers/querysets to simplify the code and centralize querying logic.
- Example: see `calendar_integration/managers.py` and `calendar_integration/querysets.py`.

## Django Virtual Models
- We use [`django-virtual-models`](https://github.com/vintasoftware/django-virtual-models) for optimizing Django ORM querysets based on the Django REST Framework Serializers attached on views/endpoints. 
- Virtual models are defined in each app's `virtual_models.py` file.
- Use virtual models for optimizing querysets in API responses. 

## Custom Views (common app)
- Shared views are defined in `common/utils/views_utils.py`.
- Use these views for reusable logic across apps.
- When creating new views, prefer class-based views/viewsets and leverage mixins from the common app.

## Multi-Tenancy
- Multi-tenancy is implemented via the `organizations` and `common` apps.
- Organization context is managed in models, views, and services.
- Always scope queries and business logic to the current organization. This is mandatory and not respecting it will lead to exceptions.
- Tenant-scoped models must inherit from the OrganizationModel abstract model class.
- Every model that inherits from OrganizationModel (are scoped by organization) should always be retrieved with the proper organization filters. The model manager will raise exceptions in case the we don't filter by organization.
- All foreign keys and one to one fields that are declared with `OrganizationForeignKey` and `OrganizationOneToOneField` actually create two fields: one concrete field using the original name appended by "_fk" and a ForeignObject field with the original name that's using the concrete field and the organization foreign key to join the tables.
- Other foreign keys and one to one fields that use default Django fields will behave as normal Django fields without the additional organization scoping.

## Custom Database functions/procedures/triggers/views/materialized views
- We have a migrations system implemented on `common/raw_sql_migration_managers.py`. It allows us to keep track of database-defined code over time.
- To create a new database-defined structure you need to create a directory with its name under the structure name directory and the numbered sql file (0001.sql) with its code. You also need to create the migration manager inheriting from one of the managers defined `common/raw_sql_migration_managers.py` in the `__init__.py` file. Then you need to create a migration including the migration manager you defined and calling its `migration` method in the `operations` array of the migration.
- To update an existing database structure you just need to add a new sql file  with the following number (four digits) containing the updated version. You also need to create a migration including the proper migration manager referencing the new version name in the operations (and calling its `migrate` method).

## Testing
- We use [`pytest`](https://docs.pytest.org/) for testing.
- Tests are located in each app's `tests/` directory.
- Write unit, integration, and functional tests for all new code.
- Use pytest fixtures for setup and teardown.
- Run tests with `pytest` from the project root.

## General Contribution Guidelines
- Follow Ruff standards, its configuration can be seen in @pyproject.toml.
- Use static typing for every class/function/method you define. Please use python type inference as much as possible to avoid redefining types.
- Document public classes, methods, and functions.
- Write meaningful commit messages.
- Open pull requests with a clear description of changes and related issues.

## Specific module instructions

### Calendar Integration
- The calculation of recurring events occurrences happens dynamically on the database based on the master event `recurrence_rule` and also its rule exceptions. The functions to generate the occurrences are defined on `calendar_integration/migrations/sql/functions/calculate_recurring_events` and `calendar_integration/migrations/sql/functions/get_event_occurrences_json`. There's also a Django ORM compatible function defined on  `calendar_integration/database_functions.py`.

#### Calendar Bundles
- **Bundle calendars** allow creating events across multiple child calendars simultaneously while maintaining a single source of truth.
- Bundle calendars are of type `CalendarType.BUNDLE` and contain multiple child calendars through the `ChildrenCalendarRelationship` model.
- Each bundle has exactly one **primary calendar** (marked with `is_primary=True` in the relationship) that hosts the actual external events.
- **Event creation behavior**:
  - Primary calendar: Gets the actual `CalendarEvent` created via external providers (Google, Outlook, etc.)
  - Internal child calendars: Get representation `CalendarEvent` instances linked to the primary via `bundle_primary_event`
  - Other provider child calendars: Get `BlockedTime` entries to block the time slot
- **Bundle relationships**: The `ChildrenCalendarRelationship` model links bundle calendars to their children with organization scoping and primary designation.
- **Availability checking**: Bundle events are automatically considered when checking availability across child calendars.
- **Event management**: Use `create_event()` with a bundle calendar, call `update_event()` or `delete_event()` passing a bundle primary event. Regular `get_calendar_events_expanded()` works for bundle calendars and automatically handles deduplication.
- **Primary calendar selection**: Must be explicitly specified when creating bundle calendars via the `primary_calendar` parameter in `create_bundle_calendar()`.

For more details, refer to the README and code comments.
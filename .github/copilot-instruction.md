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
- Shared views are defined in `common/views.py`.
- Use these views for reusable logic across apps.
- When creating new views, prefer class-based views/viewsets and leverage mixins from the common app.

## Multi-Tenancy
- Multi-tenancy is implemented via the `organizations` and `common` apps.
- Organization context is managed in models, views, and services.
- Always scope queries and business logic to the current organization. This is mandatory and not respecting it will lead to exceptions.
- Tenant-scoped models must inherit from the OrganizationModel abstract model class.

## Testing
- We use [`pytest`](https://docs.pytest.org/) for testing.
- Tests are located in each app's `tests/` directory.
- Write unit, integration, and functional tests for all new code.
- Use pytest fixtures for setup and teardown.
- Run tests with `pytest` from the project root.

## General Contribution Guidelines
- Follow PEP8 and our code style conventions.
- Document public classes, methods, and functions.
- Write meaningful commit messages.
- Open pull requests with a clear description of changes and related issues.

For more details, refer to the README and code comments.

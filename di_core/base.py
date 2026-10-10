from dependency_injector import containers, providers


class BaseContainer(containers.DeclarativeContainer):
    """Root of every domain container; the only place ``config`` is declared.

    Conventions for the domain containers that inherit from it:

    - Reference an upstream provider as ``Upstream.provider`` (for example
      ``AuditContainer.audit_service``). Inherited providers are not names in a
      subclass body.
    - Reference configuration as ``BaseContainer.config.X``. Never declare a second
      ``providers.Configuration()``: it would silently shadow this one.
    - Never redeclare a provider name that another container owns. A later
      declaration replaces the earlier one without any error.
    """

    config = providers.Configuration()

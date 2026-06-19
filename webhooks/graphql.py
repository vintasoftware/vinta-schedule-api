import datetime

import strawberry
import strawberry_django

from webhooks.models import WebhookConfiguration, WebhookEvent


@strawberry_django.type(WebhookConfiguration)
class WebhookConfigurationGraphQLType:
    """GraphQL type for outgoing webhook configurations."""

    id: strawberry.auto  # noqa: A003
    event_type: strawberry.auto
    url: strawberry.auto
    headers: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(WebhookEvent)
class WebhookEventGraphQLType:
    """Read-only GraphQL type for outgoing webhook delivery history."""

    id: strawberry.auto  # noqa: A003
    event_type: strawberry.auto
    url: strawberry.auto
    status: strawberry.auto
    response_status: strawberry.auto
    retry_number: strawberry.auto
    send_after: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def configuration_id(self) -> int:
        """Return the id of the associated webhook configuration."""
        return self.configuration_fk_id  # type: ignore[attr-defined]

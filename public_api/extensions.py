from collections.abc import Iterable

from django.conf import settings
from django.http import HttpRequest

from graphql.error import GraphQLError
from pyrate_limiter import (
    BucketFullException,
    Duration,
    Rate,
)
from strawberry.extensions import SchemaExtension
from strawberry.utils.await_maybe import AsyncIteratorOrIterator

from common.redis import ResilientLimiter


class OrganizationRateLimiter(SchemaExtension):
    """
    Uses redis and the leaky bucket algorithm to limit the number of requests a organization can make
    within a specified period.
    This is useful for preventing abuse and ensuring fair usage of resources across organizations.

    Redis is optional: a process-wide circuit breaker guards every Redis call and,
    when Redis is unconfigured or down, the limiter falls back to an in-process
    bucket so the public API keeps serving requests.
    """

    limiter: ResilientLimiter | None
    rates: Iterable[Rate] | None

    def __init__(self, rates: Iterable[Rate] | None = None):
        self.rates = rates
        resolved_rates = list(rates or self.get_default_rates())
        if resolved_rates:
            self.limiter = ResilientLimiter(
                resolved_rates,
                bucket_key=getattr(settings, "PUBLIC_API_RATE_LIMITER_KEY", "public_api"),
                raise_when_fail=True,
                name="public_api",
                redis_url=getattr(settings, "PUBLIC_API_REDIS_URL", "") or None,
            )
        else:
            self.limiter = None

    @staticmethod
    def get_default_rates() -> Iterable[Rate]:
        return [
            *(
                [
                    Rate(
                        getattr(settings, "PUBLIC_API_REQUESTS_PER_SECOND_LIMIT", 0),
                        Duration.SECOND,
                    )
                ]
                if hasattr(settings, "PUBLIC_API_REQUESTS_PER_SECOND_LIMIT")
                and bool(getattr(settings, "PUBLIC_API_REQUESTS_PER_SECOND_LIMIT", 0))
                else []
            ),
            *(
                [
                    Rate(
                        getattr(settings, "PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT", 0),
                        Duration.MINUTE,
                    )
                ]
                if hasattr(settings, "PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT")
                and bool(getattr(settings, "PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT", 0))
                else []
            ),
            *(
                [
                    Rate(
                        getattr(settings, "PUBLIC_API_REQUESTS_PER_HOUR_LIMIT", 0),
                        Duration.HOUR,
                    )
                ]
                if hasattr(settings, "PUBLIC_API_REQUESTS_PER_HOUR_LIMIT")
                and bool(getattr(settings, "PUBLIC_API_REQUESTS_PER_HOUR_LIMIT", 0))
                else []
            ),
        ]

    def on_execute(self) -> AsyncIteratorOrIterator[None]:
        """
        This method is called on each request.
        It checks if the organization has exceeded the allowed number of requests.

        It uses yield to control the flow of execution.
        """
        context = self.execution_context.context
        request: HttpRequest = context.request

        # request.public_api_system_user is set by public_api.middlewares.PublicApiSystemUserMiddleware
        organization = getattr(request, "public_api_organization", None)
        organization_id = organization.id if organization else None

        if organization_id is None or self.limiter is None:
            yield
            return None

        try:
            self.limiter.try_acquire(str(organization_id))
            yield
        except BucketFullException as e:
            raise GraphQLError(
                "Rate-limit exhausted. Please wait for some time before trying again."
            ) from e
        except Exception:  # noqa: BLE001
            # in case we're unable to connect to Redis or any other error occurs let the request go through
            # This is to ensure that the application remains functional even if the rate-limiting service is down.
            yield

        return None

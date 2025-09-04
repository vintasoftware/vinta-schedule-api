from collections.abc import Iterable

from django.conf import settings
from django.http import HttpRequest

from graphql.error import GraphQLError
from pyrate_limiter import (
    BucketFullException,
    Duration,
    Limiter,
    Rate,
    RedisBucket,
)
from redis import Redis
from redis.exceptions import RedisError
from strawberry.extensions import SchemaExtension
from strawberry.utils.await_maybe import AsyncIteratorOrIterator


class OrganizationRateLimiter(SchemaExtension):
    """
    Uses redis and the leaky bucket algorithm to limit the number of requests a organization can make
    within a specified period.
    This is useful for preventing abuse and ensuring fair usage of resources across organizations.
    """

    redis_client: Redis | None
    rates: Iterable[Rate] | None

    def __init__(self, rates: Iterable[Rate] | None = None):
        self.rates = rates
        try:
            self.redis_client = Redis.from_url(getattr(settings, "PUBLIC_API_REDIS_URL", ""))
        except RedisError:
            self.redis_client = None

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

        if organization_id is None:
            yield
            return None
        if not self.redis_client:
            yield
            return None

        bucket = RedisBucket.init(
            list(self.rates or self.get_default_rates()),
            self.redis_client,
            getattr(settings, "PUBLIC_API_RATE_LIMITER_KEY", ""),
        )
        limiter = Limiter(bucket)

        try:
            limiter.try_acquire(organization_id)
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

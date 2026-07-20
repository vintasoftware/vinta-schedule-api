from collections.abc import Iterable

from django.conf import settings
from django.http import HttpRequest

from graphql.error import GraphQLError
from pyrate_limiter import (
    Duration,
    Rate,
)
from rest_framework.views import set_rollback
from strawberry.extensions import SchemaExtension
from strawberry.utils.await_maybe import AsyncIteratorOrIterator

from common.redis import ResilientLimiter
from payments.exceptions import OverLimitError


def over_limit_graphql_error(exc: OverLimitError) -> GraphQLError:
    """Render ``OverLimitError`` as a GraphQL error, byte-identical to the REST body.

    The shared over-limit contract (``OverLimitError.as_error_body()``) is carried
    verbatim in the GraphQL error's ``extensions`` — the GraphQL spec's own
    mechanism for attaching structured, machine-readable data to an error — so a
    client handling the REST 402 body and a client handling this error's
    ``extensions`` see the identical ``detail`` / ``code`` / ``resource`` /
    ``current_usage`` / ``limit`` / ``remedy`` fields without either surface
    restating the shape (mirrors ``common.exception_handlers.vinta_exception_handler``,
    which renders the same dict as the REST response body).

    Also rolls back the request transaction. Under ``ATOMIC_REQUESTS``, a REST
    view relies on an *unhandled* exception propagating out of the view to
    trigger a rollback — which is exactly what
    ``common.exception_handlers.vinta_exception_handler`` compensates for by
    calling ``set_rollback()`` before returning a ``Response``. GraphQL has the
    same problem for a different reason: graphql-core catches every resolver
    exception internally and always returns a normal 200 response with the
    error embedded in ``errors``, so the view itself never sees an exception to
    propagate. Without this, a write a guarded service made before it reached
    the limit check (e.g. ``invite_user_to_organization``'s invitation row)
    would commit while the client is told the request was rejected.
    """
    set_rollback()
    return GraphQLError(exc.detail, extensions=exc.as_error_body())


class OrganizationRateLimiter(SchemaExtension):
    """Rate-limit organization and anonymous requests.

    Uses redis and the leaky bucket algorithm to limit the number of requests
    an organization or IP can make within a specified period.
    This is useful for preventing abuse and ensuring fair usage of resources
    across organizations.

    Redis is optional: a process-wide circuit breaker guards every Redis call
    and, when Redis is unconfigured or down, the limiter falls back to an
    in-process bucket so the public API keeps serving requests.
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
        """Rate-limit the request (by org ID or client IP).

        This method is called on each request and checks if the organization
        (or IP for anonymous requests) has exceeded the allowed number of
        requests.

        For authenticated requests, uses the organization ID.
        For unauthenticated requests, uses the client's IP address.

        It uses yield to control the flow of execution.
        """
        context = self.execution_context.context
        request: HttpRequest = context.request

        # public_api_system_user set by PublicApiSystemUserMiddleware
        organization = getattr(request, "public_api_organization", None)
        organization_id = organization.id if organization else None

        if self.limiter is None:
            yield
            return None

        # Determine rate-limit key: org ID for authenticated, IP for anonymous
        if organization_id is not None:
            rate_limit_key = str(organization_id)
        else:
            # Get client IP from headers (respects X-Forwarded-For behind proxy)
            # NAT/XFF Tradeoff (v1):
            # - Clients behind shared NAT or a CDN that doesn't forward per-client XFF
            #   will share one anon rate-limit bucket (reduces granularity but acceptable
            #   for public branding endpoint with vinta-default fallback).
            # - XFF is client-controlled and spoofable, so anon-limit evasion/forgery
            #   is accepted in v1 (impact is low: the endpoint returns only public branding
            #   with a vinta-default fallback, so unauthorized access doesn't expose secrets).
            # - Trusted-proxy-count-aware IP derivation is deferred (requires ops input
            #   on real proxy topology).
            client_ip = request.headers.get("X-Forwarded-For", "").split(",")[
                0
            ].strip() or request.META.get("REMOTE_ADDR", "")
            rate_limit_key = f"anon:{client_ip}"

        try:
            # pyrate-limiter 4 is non-blocking and returns a bool instead of
            # raising BucketFullException when the limit is exhausted.
            acquired = self.limiter.try_acquire(rate_limit_key)
        except Exception:  # noqa: BLE001
            # If Redis is unreachable or error occurs, allow request. Ensures
            # the API remains functional even when rate-limiting is down.
            yield
            return None

        if not acquired:
            raise GraphQLError(
                "Rate-limit exhausted. Please wait for some time before trying again."
            )

        yield
        return None

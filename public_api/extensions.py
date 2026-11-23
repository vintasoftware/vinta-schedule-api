import functools
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, NoReturn

from django.conf import settings
from django.http import HttpRequest

from dependency_injector.wiring import Provide, inject
from graphql.error import GraphQLError
from pyrate_limiter import (
    Duration,
    Rate,
)
from rest_framework.views import set_rollback
from strawberry.extensions import SchemaExtension
from strawberry.types.graphql import OperationType
from strawberry.utils.await_maybe import AsyncIteratorOrIterator
from vinta_billing.exceptions import OverLimitError

from common.redis import ResilientLimiter
from common.utils.request_utils import proxied_client_ip
from public_api.constants import ON_BEHALF_OF_HEADER
from public_api.exceptions import OnBehalfOfMemberError, PublicAPIServiceUnavailableError


if TYPE_CHECKING:
    from public_api.services import PublicAPIAuthService


def raise_over_limit_graphql_error(exc: OverLimitError) -> NoReturn:
    """Roll back the request transaction and raise ``OverLimitError`` as a
    GraphQL error, byte-identical to the REST body.

    A raising function, not a value-returning one that a caller might forget to
    ``raise``: this has a side effect (the rollback below), so a call site that
    wrote ``over_limit_graphql_error(exc)`` without ``raise`` in front of it
    would silently roll back the transaction and then fall through as if the
    request had succeeded. Making the function itself raise removes that
    footgun — there is no way to call it and continue.

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

    ``set_rollback()`` marks the **whole request's** transaction for rollback, not
    just the current field. GraphQL executes a mutation document's root-level
    fields serially in one transaction, so a document with more than one root
    mutation field — e.g. ``mutation { a: createCalendar(...) b:
    createInvitation(...) }`` where ``b`` hits this — rolls back ``a``'s write too,
    even though the response still reports 200 with ``data.a`` populated: the
    client is told ``a`` succeeded when its write did not survive. This is
    documented rather than guarded against — rejecting multi-root-field
    documents outright is out of scope here.
    """
    set_rollback()
    raise GraphQLError(exc.detail, extensions=exc.as_error_body()) from exc


def _rates_from_settings(prefix: str) -> list[Rate]:
    """Read ``<prefix>_PER_{SECOND,MINUTE,HOUR}_LIMIT``. A missing or zero limit
    adds no rate for that window."""
    windows = (("SECOND", Duration.SECOND), ("MINUTE", Duration.MINUTE), ("HOUR", Duration.HOUR))
    return [
        Rate(limit, duration)
        for window, duration in windows
        if (limit := getattr(settings, f"{prefix}_PER_{window}_LIMIT", 0))
    ]


def _build_limiter(rates: list[Rate], bucket_key: str) -> ResilientLimiter | None:
    if not rates:
        return None
    return ResilientLimiter(
        rates,
        bucket_key=bucket_key,
        name=bucket_key,
        redis_url=getattr(settings, "PUBLIC_API_REDIS_URL", "") or None,
        per_name=True,
    )


@functools.cache
def _shared_limiters() -> tuple[ResilientLimiter | None, ResilientLimiter | None]:
    """The ``(organization, anonymous)`` limiters, built once per process.

    Strawberry instantiates the extension on every request. Building the limiters
    there would load the Redis script on every request and, with Redis down, give
    each request a fresh in-memory bucket that never fills.
    """
    bucket_key = getattr(settings, "PUBLIC_API_RATE_LIMITER_KEY", "public_api")
    return (
        _build_limiter(_rates_from_settings("PUBLIC_API_REQUESTS"), bucket_key),
        _build_limiter(_rates_from_settings("PUBLIC_API_ANON_REQUESTS"), bucket_key),
    )


class OrganizationRateLimiter(SchemaExtension):
    """Rate-limit organization and anonymous requests.

    Each organization gets its own budget (``PUBLIC_API_REQUESTS_PER_*``), and so
    does each anonymous client IP, under the lower ``PUBLIC_API_ANON_REQUESTS_PER_*``
    limits. Budgets live in Redis so every process shares them.

    Redis is optional: a process-wide circuit breaker guards every Redis call
    and, when Redis is unconfigured or down, the limiter falls back to an
    in-process bucket so the public API keeps serving requests.
    """

    limiter: ResilientLimiter | None
    anonymous_limiter: ResilientLimiter | None

    def __init__(
        self,
        rates: Iterable[Rate] | None = None,
        anonymous_rates: Iterable[Rate] | None = None,
    ):
        self.limiter, self.anonymous_limiter = _shared_limiters()
        bucket_key = getattr(settings, "PUBLIC_API_RATE_LIMITER_KEY", "public_api")
        if rates is not None:
            self.limiter = _build_limiter(list(rates), bucket_key)
        if anonymous_rates is not None:
            self.anonymous_limiter = _build_limiter(list(anonymous_rates), bucket_key)

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

        # Organization ID for authenticated requests, client IP for anonymous ones.
        if organization_id is not None:
            limiter = self.limiter
            rate_limit_key = str(organization_id)
        else:
            limiter = self.anonymous_limiter
            # Client IP as the trusted proxies in front of the app reported it. Only
            # the entries those proxies wrote count, so a client cannot pick its
            # own bucket by sending a forged X-Forwarded-For (see
            # common.utils.request_utils.proxied_client_ip).
            # Clients behind a shared NAT still share one anonymous bucket. That
            # is accepted: the anonymous surface is public branding with a
            # vinta-default fallback.
            client_ip = proxied_client_ip(request.META) or request.META.get("REMOTE_ADDR", "")
            rate_limit_key = f"anon:{client_ip}"

        if limiter is None:
            yield
            return None

        try:
            # pyrate-limiter 4 is non-blocking and returns a bool instead of
            # raising BucketFullException when the limit is exhausted.
            acquired = limiter.try_acquire(rate_limit_key)
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


class OnBehalfOfMemberExtension(SchemaExtension):
    """Bind a mutation's ``X-Public-Api-On-Behalf-Of`` claim for the audit trail.

    An organization-wide or org-less token can name the organization member it
    is acting for. On a mutation, ``PublicAPIAuthService.on_behalf_of_claim``
    checks the header, and every audit record the token writes while the
    mutation runs carries the member as a claim. The token stays the actor and
    keeps exactly the access it had.

    The header is ignored, without being checked, when:

    * the operation is not a mutation, because only writes are audited;
    * the token is scoped to a member, because its member is already recorded;
    * an org-less token chose no organization, so there is none to check against.

    A refused header becomes a GraphQL error with the error's ``code`` in
    ``extensions``, raised before any resolver runs, so nothing is written.

    Runs inside ``PublicApiSystemUserMiddleware``'s ``organization_context``.
    """

    @inject
    def _auth_service(
        self,
        service: Annotated[
            "PublicAPIAuthService | None", Provide["public_api_auth_service"]
        ] = None,
    ) -> "PublicAPIAuthService":
        # Same shape as ``PublicApiSystemUserMiddleware``: a broken DI wiring fails
        # loudly instead of skipping the check.
        if service is None:
            raise PublicAPIServiceUnavailableError()
        return service

    def on_execute(self) -> AsyncIteratorOrIterator[None]:
        """Check the header on mutations, then run the operation with the claim bound."""
        request: HttpRequest = self.execution_context.context.request
        system_user = getattr(request, "public_api_system_user", None)
        organization = getattr(request, "public_api_organization", None)

        if (
            system_user is None
            or organization is None
            or system_user.scoped_to_membership_user_id is not None
            or self.execution_context.operation_type != OperationType.MUTATION
        ):
            yield
            return None

        try:
            claim = self._auth_service().on_behalf_of_claim(
                system_user, organization, request.headers.get(ON_BEHALF_OF_HEADER)
            )
        except OnBehalfOfMemberError as error:
            raise GraphQLError(error.message, extensions={"code": error.code.value}) from error

        with claim:
            yield
        return None

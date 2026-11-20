"""The member a public API token says it is acting for, for the current request.

An organization-wide or org-less token can name an organization member in the
``X-Public-Api-On-Behalf-Of`` header. Once the header has been checked, the
claim is bound here for the duration of the request, and
``OrganizationAuditService.actor_from_system_user`` reads it when it builds the
audit actor.

A context variable rather than an attribute on the ``SystemUser`` instance: the
calendar services receive the token through several paths, and some of them
hold a different instance of the same row. An attribute would be lost on those
paths. The claim also records the id of the token it belongs to, and the reader
ignores it for any other token, so it can never end up on another actor.

This module imports nothing from Django on purpose, so the audit service can
import it without an import cycle.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class OnBehalfOfClaim:
    """A token's claim that it is acting for one organization member.

    This is what the token says, not something we verified beyond the member
    existing in the organization. The audit trail records it as a claim and
    keeps the token as the actor.

    Attributes:
        system_user_id: The token that made the claim.
        membership_user_id: The member's user id inside the organization.
        label: The member's display label at the time of the request.
    """

    system_user_id: int
    membership_user_id: int
    label: str


_current_claim: ContextVar[OnBehalfOfClaim | None] = ContextVar(
    "public_api_on_behalf_of_claim", default=None
)


@contextmanager
def on_behalf_of_claim(claim: OnBehalfOfClaim | None) -> Iterator[None]:
    """Bind ``claim`` for the duration of the block, then restore the previous value.

    The previous value is restored on every exit path, including an exception,
    so a claim never leaks into the next request a worker serves.
    """
    token = _current_claim.set(claim)
    try:
        yield
    finally:
        _current_claim.reset(token)


def current_on_behalf_of_claim() -> OnBehalfOfClaim | None:
    """Return the claim bound for the current request, or ``None``."""
    return _current_claim.get()

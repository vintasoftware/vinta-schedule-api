"""The two seat checks that have to exclude a pending invitation.

``organization_members`` is the one resource whose counter reads per-call data:
``UsageContext.extra["exclude_invitation_id"]`` names an invitation to leave out
of the pending count (see ``payments.seams.resources``). Two call sites need it,
and both need it for the same reason -- what they are about to do is **net zero**
on seats:

- **Accepting** an invitation. The pending invitation stops being pending and
  becomes the membership it was already holding a seat for. Counted on both
  sides, the accept fails its own ``delta=1`` check at exactly the ceiling, and
  an organization can invite up to its limit and then never let anybody in.
- **Resending** one. ``get_or_create`` reuses the still-pending row rather than
  writing a new one, so a resend at the exact ceiling is a false block.

Both are wrapped in a named function here rather than spelled as
``check_limit(..., usage_extra_resolver=...)`` at the call site, and that is the
point of this module. Getting it wrong the other way is a **missing keyword
argument**: invisible in review, ungreppable, and silent -- the caller gets a
count computed as though nothing had been excluded, believes it, and refuses a
member the organization has room for. Getting it wrong here is a missing call,
which is none of those things. The host's pre-migration ``EntitlementService``
made the same argument for its own ``check_seat_limit_for_invitation_accept``;
these functions are that method and its resend sibling, rebuilt on the
package's ``usage_extra_resolver``.

**Laziness is not incidental.** ``usage_extra_resolver`` is called at most once
and only after the ceiling is known to be finite, so an ``unlimited``
organization -- which is every organization for the length of this rollout, and
the case where the cost would be paid on every request -- never runs the query
that decides which invitation to exclude.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vinta_billing.services.entitlement_service import EntitlementService, LimitCheckResult

from payments.seams.resources import ORGANIZATION_MEMBERS


if TYPE_CHECKING:
    from organizations.models import Organization, OrganizationInvitation


#: The single ``usage_extra`` key ``organization_members``' counter reads.
#: Declared on that resource's registration, so aiming it at any other resource
#: raises ``vinta_billing.exceptions.InapplicableUsageExtraError`` instead of
#: being silently ignored.
EXCLUDE_INVITATION_ID = "exclude_invitation_id"


def check_seat_limit_for_invitation_accept(
    entitlement_service: EntitlementService,
    invitation: OrganizationInvitation,
    lock: bool = True,
) -> LimitCheckResult:
    """May ``invitation`` be accepted without exceeding the seat ceiling?

    ``lock`` defaults to ``True`` -- unlike ``check_limit`` -- because this is
    only ever called immediately before the accept writes, which is exactly the
    situation the row lock exists for. See ``EntitlementService.check_limit``
    for the transaction and isolation-level requirements that come with it.

    The exclusion is passed eagerly (``usage_extra``, not the resolver): the
    invitation is already in hand, so there is no query to defer.
    """
    return entitlement_service.check_limit(
        invitation.organization,
        ORGANIZATION_MEMBERS,
        delta=1,
        lock=lock,
        usage_extra={EXCLUDE_INVITATION_ID: invitation.pk},
    )


def check_seat_limit_for_invitation_send(
    entitlement_service: EntitlementService,
    organization: Organization,
    resolve_reused_invitation_id: Callable[[], int | None],
    lock: bool = True,
) -> LimitCheckResult:
    """May one more seat be invited into ``organization``?

    ``resolve_reused_invitation_id`` answers "is this a resend, and of which
    row?" -- ``None`` for a genuinely new invitation, which is then checked and
    blocked at the ceiling exactly as before. It is a callable rather than an
    id because answering it is itself a query, and on the unlimited path usage
    is not counted at all; see the module docstring.
    """

    def _usage_extra() -> dict[str, Any]:
        return {EXCLUDE_INVITATION_ID: resolve_reused_invitation_id()}

    return entitlement_service.check_limit(
        organization,
        ORGANIZATION_MEMBERS,
        lock=lock,
        usage_extra_resolver=_usage_extra,
    )

"""Writes this project's audit trail from the billing engine's signals.

``SubscriptionService.set_payment_provider`` used to hold an ``OrganizationAuditService``
and call ``record()`` inline. ``vinta_billing``'s copy cannot: a library has no
way to know a project keeps an audit log, so it publishes
``vinta_billing.signals.payment_provider_repointed`` at the same point --
after the write, inside the caller's transaction -- with the same facts the
inline call used (the profile, the organization, the acting user, the old and
new provider slugs). Connecting a receiver here is how the trail stays intact.

Only the repoint is connected. It is the one billing write the host audited
before the move (see the ``audit`` app's rollout notes: billing writes were
otherwise deliberately out of scope), and this seam deliberately does not widen
that scope while relocating it. The eight other signals the package publishes
are available if that decision changes.

The receiver runs inside the caller's transaction, exactly where the inline
call did, so a failure here still rolls the repoint back with it rather than
leaving a repoint nobody can account for. ``OrganizationAuditService.record`` itself defers
the write to a Celery task through ``transaction.on_commit``, so the slow part
was never in the transaction to begin with.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model
from django.dispatch import receiver

from vinta_billing.signals import payment_provider_repointed

from audit_integration.constants import AuditAction


@receiver(payment_provider_repointed, dispatch_uid="payments.seams.audit.record_repoint")
def record_payment_provider_repoint(
    sender: type[Model],
    billing_profile: Any,
    organization: Any,
    actor: Any,
    from_provider: str,
    to_provider: str,
    **kwargs: Any,
) -> None:
    """Record a ``BillingProfile.payment_provider`` repoint as an UPDATE entry.

    ``actor`` is the staff member driving the repoint when there is a request
    behind it (``BillingProfileAdmin.save_model`` passes ``request.user``), and
    ``None`` for an operator running it by hand or a script -- resolved to a
    MEMBERSHIP and a SYSTEM actor respectively, which is what the pre-move
    inline call did.
    """
    # Deferred import: `di_core.containers.container` is only assigned in
    # `DICoreConfig.ready()`, so a module-level import binds `None`.
    from di_core.containers import container

    if container is None:
        raise RuntimeError(
            "DI container is not wired; the billing audit seam cannot resolve "
            "audit_service before di_core.apps.DICoreConfig.ready() runs."
        )

    audit_service = container.audit_service()
    actor_snapshot = (
        audit_service.actor_from_user(actor, organization.pk)
        if actor is not None
        else audit_service.system_actor()
    )
    audit_service.record(
        action=AuditAction.UPDATE,
        actor=actor_snapshot,
        subject=audit_service.subject_from_instance(billing_profile),
        diff={"payment_provider": {"old": from_provider, "new": to_provider}},
        scope=audit_service.scope_from_organization_id(organization.pk),
    )

"""Adapter satisfying ``vinta_billing.notifications.Notifier`` over the host's
vintasend ``NotificationService``.

Every ``vinta_billing`` service that sends a dunning or usage-warning message
takes a notifier as a constructor argument and falls back to whatever
``VINTA_BILLING['NOTIFIER']`` configures. ``vinta_billing.notifications.Notifier``
is a one-method protocol modelled directly on
``vintasend.services.notification_service.NotificationService.create_notification``'s
own signature, so this adapter has nothing to translate -- it resolves the
instance the DI container already builds (the same one every other
notification-sending service in this project uses) and forwards every
argument by name.

``payments.notification_contexts`` keeps registering its
``@register_context`` context functions exactly as it does today: the engine
passes ``context_name`` / ``context_kwargs`` straight through to
``NotificationService.create_notification``, which resolves the context by
that same registry.
"""

from __future__ import annotations

import datetime
from typing import Any

from vintasend.services.notification_service import NotificationContextDict


class NotificationServiceNotifier:
    """``vinta_billing.notifications.Notifier`` over the DI-built ``NotificationService``.

    Resolved lazily from the container on each call, not injected at
    construction: ``vinta_billing`` instantiates its configured ``NOTIFIER``
    itself (see ``vinta_billing.notifications.get_notifier``), so there is no
    DI seam on the package's side to inject into -- this class has to reach
    for the container the same way every other non-request-bound call site in
    this project does.
    """

    def create_notification(
        self,
        user_id: Any,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, Any],
        subject_template: str | None = None,
        preheader_template: str | None = None,
        send_after: datetime.datetime | None = None,
    ) -> Any:
        # Deferred import: `di_core.containers.container` is only assigned in
        # `DICoreConfig.ready()`, so a module-level import binds `None`.
        from di_core.containers import container

        if container is None:
            raise RuntimeError(
                "DI container is not wired; NotificationServiceNotifier cannot resolve "
                "notification_service before di_core.apps.DICoreConfig.ready() runs."
            )
        notification_service = container.notification_service()
        return notification_service.create_notification(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            body_template=body_template,
            context_name=context_name,
            # `NotificationService.create_notification` wants its own
            # `dict` subclass, not a plain `dict[str, Any]` -- see
            # `vinta_billing.services.dunning_service` for the same wrapping.
            context_kwargs=NotificationContextDict(context_kwargs),
            send_after=send_after,
            subject_template=subject_template or "",
            preheader_template=preheader_template or "",
        )

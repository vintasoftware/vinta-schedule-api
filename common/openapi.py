"""Custom drf-spectacular schema classes for the Vinta Schedule API.

``TenantScopedAutoSchema`` extends the default ``AutoSchema`` to inject the
``X-Organization-Id`` header parameter on every operation whose view is a
:class:`~common.utils.view_utils.TenantScopedViewMixin` subclass, unless the
operation is opted out via ``active_org_resolution_optional`` or
``active_org_optional_actions``.

This is the centralized mechanism: one class auto-covers every current and
future ``TenantScopedViewMixin`` route without requiring per-view
``@extend_schema`` annotations.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes
from rest_framework.serializers import Serializer

from common.utils.view_utils import ACTIVE_ORG_HEADER, TenantScopedViewMixin


_HEADER_DESCRIPTION = (
    "Selects the active organization for this request. "
    "Optional for callers that belong to exactly one active organization — "
    "the single membership is resolved implicitly. "
    "**Required** when the caller has two or more active memberships; "
    "omitting it in that case returns **400**. "
    "If the header names an organization the caller is not an active member of, "
    "the server returns **403**."
)


class TenantScopedAutoSchema(AutoSchema):
    """drf-spectacular ``AutoSchema`` that documents ``X-Organization-Id`` on tenant-scoped routes.

    For every operation whose view is a :class:`~common.utils.view_utils.TenantScopedViewMixin`
    subclass, this class appends an ``X-Organization-Id`` header parameter to
    the operation's parameter list — **unless** the view or action has opted out
    of strict org resolution via ``active_org_resolution_optional = True`` or
    by listing the current action in ``active_org_optional_actions``.

    The ``required`` flag is ``False`` because single-membership callers may
    omit the header.  The description explains the full resolution contract.
    """

    def get_override_parameters(
        self,
    ) -> list[OpenApiParameter | Serializer[Any] | type[Serializer[Any]]]:
        """Extend the base parameter list with ``X-Organization-Id`` when appropriate."""
        params: list[OpenApiParameter | Serializer[Any] | type[Serializer[Any]]] = list(
            super().get_override_parameters()
        )

        view = self.view
        if not isinstance(view, TenantScopedViewMixin):
            return params

        # Check view-level opt-out.
        if getattr(view, "active_org_resolution_optional", False):
            return params

        # Check per-action opt-out.
        current_action: str | None = getattr(view, "action", None)
        optional_actions: tuple[str, ...] = getattr(view, "active_org_optional_actions", ())
        if current_action is not None and current_action in optional_actions:
            return params

        params.append(
            OpenApiParameter(
                name=ACTIVE_ORG_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=False,
                description=_HEADER_DESCRIPTION,
            )
        )
        return params

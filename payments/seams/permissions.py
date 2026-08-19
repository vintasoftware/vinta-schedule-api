"""Restores the object-level half of the billing permission check.

**This module exists for a defect in ``vinta-django-billing`` 0.4.0, and should
be deleted the release it is fixed in.** Reported with this phase; see the phase
report's package-gap section.

``vinta_billing.permissions.IsBillingManager.has_object_permission`` reads
``getattr(obj, "organization", None)`` and, finding nothing, falls back to
``has_permission`` -- i.e. to the organization the *request* resolved. Every
object-level check the package's own viewsets make passes an **Organization**
(the billing root, from ``resolve_billing_root``):
``MeteredOccurrenceViewSet.list``, ``SubscriptionViewSet.get_object`` and
``AddOnViewSet.create`` all do. An ``Organization`` has no ``.organization``, so
all three degrade to the coarse request-level check and the DENY branch can
never fire.

What that costs, concretely: an administrator of a child organization that pools
against a reseller root it does not administer passes the coarse check (it does
administer *something*), reaches the object check, and is allowed to change the
**root's** plan and buy add-ons against the root's subscription. The package's
own suite cannot catch it -- it has no viewset tests at all, which is how the
0.3.0 mounting defect shipped too.

The host has always had the right answer for this project, in
``organizations.permissions.IsBillingOwnerOrAdmin``, whose object-level branch
also covers the acting-reseller-root case the package has no notion of. So the
fix here is to keep the package's coarse gate (which is what
``BILLING_MANAGER_PREDICATE`` configures, and which is correct) and delegate
only the object-level question back to the host's class.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView
from vinta_billing.permissions import IsBillingManager

from organizations.permissions import IsBillingOwnerOrAdmin


class OrganizationAwareIsBillingManager(IsBillingManager):
    """``IsBillingManager``'s coarse gate, with a working object-level gate."""

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        return IsBillingOwnerOrAdmin().has_object_permission(request, view, obj)


def with_working_object_permission(permissions: list[BasePermission]) -> list[BasePermission]:
    """Swap every ``IsBillingManager`` in ``permissions`` for the fixed subclass.

    Applied to whatever a viewset's ``get_permissions()`` returned rather than
    to its ``permission_classes``, because the package spells the same
    permission both ways -- ``MeteredOccurrenceViewSet`` and ``AddOnViewSet``
    declare it as a class attribute, while ``SubscriptionViewSet`` and
    ``BillingProfileViewSet`` build it per action in ``get_permissions()``.
    Rewriting the resolved list is the one place both spellings pass through.
    """
    return [
        OrganizationAwareIsBillingManager() if isinstance(p, IsBillingManager) else p
        for p in permissions
    ]

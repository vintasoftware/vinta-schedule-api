"""Audit administration — repository-backed, read-only changelist.

Architecture
------------
We register an ``AuditAdmin(admin.ModelAdmin)`` for the ``Audit`` model so the
changelist appears in the Django admin index with standard auth / permission /
breadcrumb handling.  The ``ModelAdmin`` is a *shell*:

- ``has_add_permission`` / ``has_change_permission`` / ``has_delete_permission``
  always return ``False`` — no create, edit, or delete is permitted.
- ``changelist_view`` is overridden to parse filter params from the request GET,
  build an ``AuditQuery``, call ``repository.query(...)`` and render a custom
  template (``admin/audit/audit/change_list.html``) with the returned
  ``AuditPage``.  Django's ORM ChangeList machinery is bypassed entirely.
- The repository is resolved at request time via
  ``di_core.containers.container.audit_repository()`` — same pattern as
  ``audit/tasks.py`` — so tests can swap the backend via
  ``container.audit_repository.override(stub)`` without touching this module.

Template path
-------------
``audit/templates/admin/audit/audit/change_list.html`` — discovered by
Django's ``app_directories.Loader``.  It also matches the template name that
``ModelAdmin`` defaults to for this model
(``admin/<app_label>/<model_name>/change_list.html``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit
from audit.types import AuditQuery


logger = logging.getLogger(__name__)

_DEFAULT_PER_PAGE = 50
_MAX_PER_PAGE = 200


def _parse_int(value: str | None) -> int | None:
    """Return an int from a string, or None if blank/invalid."""
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO datetime string (or date string) to a timezone-aware datetime.

    Accepts:
    - ``YYYY-MM-DDTHH:MM`` (datetime-local input)
    - ``YYYY-MM-DD`` (date input — interpreted as start of that day UTC)

    Returns None on parse failure so the filter is silently skipped.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_has_diff(value: str | None) -> bool | None:
    """Map tri-state string to bool | None.

    "yes"  → True   (only records WITH a diff)
    "no"   → False  (only records WITHOUT a diff)
    ""     → None   (no filter — show all)
    """
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _build_audit_query(params: dict[str, str | list[str]]) -> AuditQuery:
    """Build an ``AuditQuery`` from request GET params.

    ``params`` must be the result of ``dict(QueryDict_instance)``, which yields
    ``dict[str, list[str]]`` (each key maps to a list of submitted values).
    Do NOT pass ``QueryDict.dict()`` — that yields scalar strings and bypasses
    the list-branch normalisation below.  Each value is normalised to a single
    non-blank string via ``_first``; empty/blank values normalise to ``None``
    so that an unset filter field is treated as "no filter".
    """

    def _first(v: str | list[str] | None) -> str | None:
        """Return the first non-blank element if v is a list, or v itself.

        The list branch applies ``or None`` so that an empty submitted value
        (e.g. ``actor_type=''`` → ``['']``) normalises to ``None`` rather
        than ``''``, which would cause the repository to filter on an empty
        string instead of skipping the filter entirely.
        """
        if v is None:
            return None
        if isinstance(v, list):
            return (v[0] or None) if v else None
        return v or None

    action = _first(params.get("action"))
    actor_type = _first(params.get("actor_type"))
    created_after = _parse_datetime(_first(params.get("created_after")))
    created_before = _parse_datetime(_first(params.get("created_before")))
    has_diff = _parse_has_diff(_first(params.get("has_diff")))
    organization_id = _parse_int(_first(params.get("organization_id")))
    search = _first(params.get("search"))
    affected_membership_id = _parse_int(_first(params.get("affected_membership_id")))

    return AuditQuery(
        organization_id=organization_id,
        actions=[action] if action else None,
        actor_type=actor_type,
        created_after=created_after,
        created_before=created_before,
        has_diff=has_diff,
        search=search,
        affected_membership_id=affected_membership_id,
    )


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    """Read-only Django admin for Audit records.

    Data is sourced exclusively from ``AuditRepository.query(...)`` so the admin
    works against ANY repository backend (ORM or otherwise).  The ModelAdmin
    provides the registration shell: auth, permission checks, admin index entry,
    and breadcrumb/nav wiring.  All row data bypasses Django's ORM ChangeList.
    """

    # Changelist template — overrides Django's default ORM-driven changelist.
    change_list_template = "admin/audit/audit/change_list.html"

    # ------------------------------------------------------------------ #
    # ORM queryset override                                              #
    # ------------------------------------------------------------------ #

    def get_queryset(self, request: HttpRequest):  # type: ignore[override]
        """Return the unscoped queryset for the Audit model.

        The tenant-scoped default manager raises ``ImproperlyConfigured`` when
        queried without an ``organization_id`` filter.  The Django admin's
        change/delete view infrastructure calls ``get_queryset`` before checking
        permissions, which would crash with a 500.  Using ``original_manager``
        (the unscoped manager added by ``OrganizationModel``) avoids that crash
        while still honouring the fact that all three permission methods return
        ``False`` (so no actual change or delete will ever occur).

        This queryset is only used by Django's internal view plumbing, NOT by the
        ``changelist_view`` override (which reads exclusively from the repository).
        """
        return Audit.original_manager.all()

    # ------------------------------------------------------------------ #
    # Read-only enforcement                                               #
    # ------------------------------------------------------------------ #

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Deny all add permissions — audit log is append-only via AuditService."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deny all change permissions — audit records are immutable."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deny all delete permissions — audit records are immutable."""
        return False

    # ------------------------------------------------------------------ #
    # Repository-backed changelist                                        #
    # ------------------------------------------------------------------ #

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        """Render the audit changelist from AuditRepository.query(...).

        Parses filter + pagination GET params, builds an ``AuditQuery``,
        calls the repository, and renders ``change_list_template`` with the
        result — bypassing Django's ORM ChangeList entirely.

        Security: requires staff status (checked by the ModelAdmin view
        dispatch via ``admin_site.admin_view``).  Non-staff requests are
        redirected to login before this method is called.
        """
        # --- pagination ---
        page = max(1, _parse_int(request.GET.get("page")) or 1)
        per_page = min(
            _MAX_PER_PAGE,
            max(1, _parse_int(request.GET.get("per_page")) or _DEFAULT_PER_PAGE),
        )
        offset = (page - 1) * per_page

        # --- filters ---
        q = _build_audit_query(dict(request.GET))

        # --- repository query (resolved at runtime for DI-overridability) ---
        from di_core import containers  # deferred to avoid import-before-wiring

        di_container = containers.container
        if di_container is None:
            logger.error("AuditAdmin.changelist_view: DI container not initialized.")
            audit_page = None
        else:
            repository = di_container.audit_repository()
            audit_page = repository.query(q, offset=offset, limit=per_page)

        # --- pagination controls ---
        total = audit_page.total if audit_page is not None else 0
        num_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
        has_prev = page > 1
        has_next = page < num_pages

        # --- choices for filter dropdowns ---
        action_choices = [("", "All actions"), *AuditAction.choices]
        actor_type_choices = [("", "All actor types"), *AuditActorType.choices]
        has_diff_choices = [
            ("", "Any"),
            ("yes", "Has diff"),
            ("no", "No diff"),
        ]

        # --- active filter values (repopulate the form fields) ---
        active_filters = {
            "action": request.GET.get("action", ""),
            "actor_type": request.GET.get("actor_type", ""),
            "created_after": request.GET.get("created_after", ""),
            "created_before": request.GET.get("created_before", ""),
            "has_diff": request.GET.get("has_diff", ""),
            "organization_id": request.GET.get("organization_id", ""),
            "search": request.GET.get("search", ""),
            "affected_membership_id": request.GET.get("affected_membership_id", ""),
        }

        # --- base querystring for pagination links (URL-safe, excludes page) ---
        # urlencode so that filter values containing & or special chars are safe.
        base_qs_params = {k: v for k, v in active_filters.items() if v}
        base_qs_params["per_page"] = str(per_page)
        base_querystring = urlencode(base_qs_params)

        context: dict[str, Any] = {
            **self.admin_site.each_context(request),
            "title": "Audit records",
            "audit_page": audit_page,
            "page": page,
            "per_page": per_page,
            "total": total,
            "num_pages": num_pages,
            "has_prev": has_prev,
            "has_next": has_next,
            "prev_page": page - 1,
            "next_page": page + 1,
            "action_choices": action_choices,
            "actor_type_choices": actor_type_choices,
            "has_diff_choices": has_diff_choices,
            "active_filters": active_filters,
            "base_querystring": base_querystring,
            # Django admin base template requires opts for breadcrumbs.
            "opts": self.model._meta,
            **(extra_context or {}),
        }
        return TemplateResponse(
            request,
            self.change_list_template,
            context,
        )

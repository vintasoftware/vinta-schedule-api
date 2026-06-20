"""Audit administration — repository-backed, read-only changelist and detail view.

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
- ``detail_view`` is a custom view registered via ``get_urls()`` that fetches
  a single audit record via ``repository.get(audit_id)`` and renders a read-only
  detail template.  If the audit is not found, HTTP 404 is returned.
- ``export_view`` is a custom view registered via ``get_urls()`` that streams
  a CSV of matching audit records, respecting active filters. The CSV is streamed
  row-by-row to bound memory usage for large result sets.
- The repository is injected via ``@inject`` / ``Provide["audit_repository"]`` on
  the ``changelist_view``, ``detail_view``, and ``export_view`` methods, matching
  the project's established DI convention.  Tests can swap the backend via
  ``container.audit_repository.override(stub)``.

Template paths
--------------
- ``audit/templates/admin/audit/audit/change_list.html`` — changelist; discovered by
  Django's ``app_directories.Loader``.  It also matches the template name that
  ``ModelAdmin`` defaults to for this model
  (``admin/<app_label>/<model_name>/change_list.html``).
- ``audit/templates/admin/audit/audit/audit_detail.html`` — detail view; custom name,
  explicitly rendered in ``detail_view``.
"""

import csv
import json
import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.http.response import Http404, HttpResponseBase, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import path

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit
from audit.repositories import AuditRepository
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

    Accepts the full range of ISO 8601 forms recognised by ``datetime.fromisoformat``
    (Python 3.11+), including:

    - ``YYYY-MM-DD`` (date only — interpreted as midnight UTC)
    - ``YYYY-MM-DDTHH:MM`` (datetime-local, no offset — treated as UTC)
    - ``YYYY-MM-DDTHH:MM:SS`` (with seconds)
    - ``YYYY-MM-DDTHH:MM:SS.ffffff`` (with microseconds)
    - ``YYYY-MM-DDTHH:MM:SSZ`` or ``YYYY-MM-DDTHH:MM:SS+00:00`` (offset-aware)

    Falls back to explicit ``strptime`` patterns for extra safety, then returns
    ``None`` on any unparseable input so the filter is silently skipped.
    """
    if not value:
        return None
    # Python 3.11+ fromisoformat handles Z suffix and offsets directly.
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result
    except ValueError:
        pass
    # Fallback: explicit strptime patterns (belt-and-suspenders).
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

    Data is sourced exclusively from ``AuditRepository.query(...)`` (changelist) and
    ``AuditRepository.get(...)`` (detail view) so the admin works against ANY
    repository backend (ORM or otherwise).  The ModelAdmin provides the registration
    shell: auth, permission checks, admin index entry, and breadcrumb/nav wiring.
    All row data bypasses Django's ORM ChangeList.
    """

    # Changelist template — overrides Django's default ORM-driven changelist.
    change_list_template = "admin/audit/audit/change_list.html"

    # Detail template — custom path for read-only detail rendering.
    detail_template = "admin/audit/audit/audit_detail.html"

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

    @inject
    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: dict[str, Any] | None = None,
        repository: Annotated[AuditRepository | None, Provide["audit_repository"]] = None,
    ) -> HttpResponse:
        """Render the audit changelist from AuditRepository.query(...).

        Parses filter + pagination GET params, builds an ``AuditQuery``,
        calls the repository, and renders ``change_list_template`` with the
        result — bypassing Django's ORM ChangeList entirely.

        The ``repository`` argument is injected via ``@inject`` /
        ``Provide["audit_repository"]``; it is not part of Django's
        ``ModelAdmin.changelist_view`` public contract and must NOT be passed
        by callers.  Tests may override the provider via
        ``container.audit_repository.override(stub)``.

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

        # --- repository query ---
        if repository is None:
            logger.error("AuditAdmin.changelist_view: repository not injected (DI not wired?).")
            audit_page = None
        else:
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

        # --- export querystring: active filters only, no per_page / page ---
        export_qs_params = {k: v for k, v in active_filters.items() if v}
        export_querystring = urlencode(export_qs_params)

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
            "export_querystring": export_querystring,
            # Django admin base template requires opts for breadcrumbs.
            "opts": self.model._meta,
            **(extra_context or {}),
        }
        return TemplateResponse(
            request,
            self.change_list_template,
            context,
        )

    # ------------------------------------------------------------------ #
    # URL routing                                                         #
    # ------------------------------------------------------------------ #

    def get_urls(self):  # type: ignore[no-untyped-def]
        """Register custom URL patterns for the detail and export views.

        Adds patterns for:
        - ``<int:audit_id>/view/`` that routes to the custom ``detail_view`` method.
        - ``export/`` that routes to the custom ``export_view`` method.
        Both are wrapped in ``admin_view`` for authentication and permission checks.
        """
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:audit_id>/view/",
                self.admin_site.admin_view(self.detail_view),
                name="audit_audit_detail",
            ),
            path(
                "export/",
                self.admin_site.admin_view(self.export_view),
                name="audit_audit_export",
            ),
        ]
        return custom_urls + urls

    # ------------------------------------------------------------------ #
    # Repository-backed detail view                                       #
    # ------------------------------------------------------------------ #

    @inject
    def detail_view(
        self,
        request: HttpRequest,
        audit_id: int,
        extra_context: dict[str, Any] | None = None,
        repository: Annotated[AuditRepository | None, Provide["audit_repository"]] = None,
    ) -> HttpResponse:
        """Render a read-only detail page for a single audit record.

        Fetches the audit via ``repository.get(audit_id)``. If not found,
        raises HTTP 404. Renders a custom template with all fields including
        a pretty-printed diff and system_user_scopes.

        The ``repository`` argument is injected via ``@inject`` /
        ``Provide["audit_repository"]``; it is not part of Django's
        ``ModelAdmin`` public contract and must NOT be passed by callers.
        Tests may override the provider via ``container.audit_repository.override(stub)``.

        Security: requires staff status (checked by the ModelAdmin view
        dispatch via ``admin_site.admin_view``).  Non-staff requests are
        redirected to login before this method is called.
        """
        if repository is None:
            logger.error("AuditAdmin.detail_view: repository not injected (DI not wired?).")
            raise Http404("Audit record not found (repository unavailable).")

        record = repository.get(audit_id)
        if record is None:
            raise Http404(f"Audit record {audit_id} not found.")

        # Format diff for readability: convert {field: {old, new}} to a list.
        formatted_diff = []
        if record.diff:
            for field_name, changes in sorted(record.diff.items()):
                formatted_diff.append(
                    {
                        "field": field_name,
                        "old": changes.get("old"),
                        "new": changes.get("new"),
                    }
                )

        # Format system_user_scopes: convert list[str] to readable list or None.
        formatted_scopes = record.actor.system_user_scopes or []

        context: dict[str, Any] = {
            **self.admin_site.each_context(request),
            "title": f"Audit record #{record.id}",
            "record": record,
            "formatted_diff": formatted_diff,
            "formatted_scopes": formatted_scopes,
            # Django admin base template requires opts for breadcrumbs.
            "opts": self.model._meta,
            **(extra_context or {}),
        }
        return TemplateResponse(
            request,
            self.detail_template,
            context,
        )

    # ------------------------------------------------------------------ #
    # CSV export view (streaming, memory-efficient)                      #
    # ------------------------------------------------------------------ #

    def _csv_row_generator(
        self,
        repository: AuditRepository | None,
        q: AuditQuery,
        chunk_size: int = 1000,
    ) -> Generator[str]:
        """Generate CSV rows from the filtered audit records.

        Pages through the repository in chunks (default 1000 per page) and yields
        CSV-encoded rows including the header row first. Memory usage is bounded
        by the chunk size (not the total result count).

        Each row encodes the following columns as CSV:
        - id, created_at (ISO format)
        - organization_id, action
        - actor_type, actor_id, actor_role
        - system_user_scopes (JSON string), system_user_scoped_to_membership
        - subject_type, subject_id, subject_label
        - affected_membership_ids (JSON string)
        - diff (JSON string)

        Args:
            repository: The AuditRepository to query (may be None on DI failure).
            q: The AuditQuery with active filters and search.
            chunk_size: Records per page (default 1000).

        Yields:
            CSV-formatted strings (one per row, including header).
        """
        if repository is None:
            logger.error("_csv_row_generator: repository not injected")
            return

        class _Echo:
            """Pseudo-buffer whose write() returns the value directly."""

            def write(self, value: str) -> str:
                return value

        writer = csv.writer(_Echo())

        # --- CSV header row ---
        header = [
            "id",
            "created_at",
            "organization_id",
            "action",
            "actor_type",
            "actor_id",
            "actor_role",
            "system_user_scopes",
            "system_user_scoped_to_membership",
            "subject_type",
            "subject_id",
            "subject_label",
            "affected_membership_ids",
            "diff",
        ]
        yield writer.writerow(header)

        # --- Paginate through all records ---
        offset = 0
        while True:
            page = repository.query(q, offset=offset, limit=chunk_size, ordering="-created_at")
            if not page.items:
                break

            for record in page.items:
                # Serialize complex fields as JSON strings; None → empty string
                system_user_scopes_json = (
                    json.dumps(record.actor.system_user_scopes)
                    if record.actor.system_user_scopes is not None
                    else ""
                )
                affected_membership_ids_json = (
                    json.dumps(record.affected_membership_ids)
                    if record.affected_membership_ids
                    else ""
                )
                diff_json = json.dumps(record.diff) if record.diff is not None else ""

                row = [
                    record.id,
                    record.created_at.isoformat(),
                    record.organization_id,
                    record.action,
                    record.actor.actor_type,
                    record.actor.actor_id or "",
                    record.actor.actor_role or "",
                    system_user_scopes_json,
                    record.actor.system_user_scoped_to_membership or "",
                    record.subject.subject_type,
                    record.subject.subject_id,
                    record.subject.subject_label or "",
                    affected_membership_ids_json,
                    diff_json,
                ]
                yield writer.writerow(row)

            offset += chunk_size
            if offset >= page.total:
                break

    @inject
    def export_view(
        self,
        request: HttpRequest,
        repository: Annotated[AuditRepository | None, Provide["audit_repository"]] = None,
    ) -> HttpResponseBase:
        """Stream a CSV export of the currently filtered/searched audits.

        Respects all active GET filters (action, actor_type, created_after/before,
        has_diff, organization_id, search, affected_membership_id) and pages through
        the repository in chunks to bound memory usage.

        Returns a StreamingHttpResponse with:
        - Content-Type: text/csv
        - Content-Disposition: attachment; filename=audit_export.csv
        - One CSV row per audit record, plus a header row.

        CSV columns (flattened from AuditRecord):
        - id, created_at, organization_id, action
        - actor_type, actor_id, actor_role
        - system_user_scopes (JSON), system_user_scoped_to_membership
        - subject_type, subject_id, subject_label
        - affected_membership_ids (JSON), diff (JSON)

        Complex fields (system_user_scopes, affected_membership_ids, diff) serialize
        as JSON strings; None values map to empty strings in the CSV.

        Security: requires staff status (checked by the ModelAdmin view dispatch
        via ``admin_site.admin_view``).  Non-staff requests are redirected to login
        before this method is called.
        """
        # --- reject non-GET methods (export is strictly read-only) ---
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])

        # --- parse filters from request GET ---
        q = _build_audit_query(dict(request.GET))

        # --- create streaming response ---
        response = StreamingHttpResponse(
            self._csv_row_generator(repository, q),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = "attachment; filename=audit_export.csv"
        return response

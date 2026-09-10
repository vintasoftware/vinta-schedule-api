"""The bridge between ``organizations.Organization`` and the ``BillingScope``
that bills it.

``vinta-django-billing`` 0.8.0 stopped taking an organization. Every service
method, permission and counter now takes a *scope* -- a row naming whoever
pays -- so that one installation can sell to an organization and to a bare user
at the same time. This project has exactly one kind of payer, an organization,
so its scope table is a mirror of its organization table and this module is
what keeps the two in step.

Three jobs, in the order they matter:

1. :func:`scope_for` -- the org-to-scope translation every call site needs.
2. :func:`sync_scope_for_organization` -- provisioning, wired to ``post_save``
   so all four organization-creation paths are covered by construction rather
   than by remembering.
3. :func:`organization_ids_for_scope_ids` -- the id-space translation the usage
   counters in ``payments.seams.resources`` need, because the engine speaks
   scope ids and this project's own tables are keyed by organization id.
   :func:`scopes_for` is its bulk counterpart in the other direction, for the
   batch entitlement reads.

**The one gap: ``QuerySet.update()`` sends no signal.** A bulk update of
``Organization.parent`` or ``can_invite_organizations`` leaves the scope mirror
stale, and the hierarchy then answers against the old tree -- silently, since
nothing raises. No production path does that today (organizations are saved one
at a time, through the service, the admin, or the reseller mutation), and this
is documented rather than defended against because the alternatives are worse:
a periodic reconcile would hide the drift it papers over, and a database trigger
would put the mirror somewhere no reader of this module would think to look. If
a bulk update of either field is ever added, call
:func:`sync_scope_for_organization` for each affected row in the same
transaction.

Why a signal rather than a call in ``OrganizationService.create_organization``:
there are four organization-creation paths (see
``organizations/tests/test_organization_creation_billing.py`` for the list --
the REST funnel, the reseller GraphQL mutation's raw ``objects.create``, the
admin, and tenant provisioning), only one of which goes through the service. A
scope that a reseller-created child never gets is not a loud failure; it is a
child that silently bills nowhere. ``post_save`` is the one hook all four share.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from vinta_billing.conf import get_scope_model

from payments.seams.hierarchy import RESELLER_ROOT_META_KEY


if TYPE_CHECKING:
    from organizations.models import Organization


def _organization_model():
    """``organizations.Organization``, resolved through the app registry.

    Not a module-level import, and not optional. ``organizations.models`` calls
    :func:`scope_for` from its branding gate, so importing it back here at
    module scope is a cycle -- ``organizations.models`` is still executing when
    this module is first imported, and ``Organization`` does not exist yet.
    Deferring the lookup to call time is the same fix ``payments.seams.resource_keys``
    documents for the same cycle.
    """
    from django.apps import apps

    return apps.get_model("organizations", "Organization")


def _organization_content_type():
    """The content type every scope this project creates points at."""
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(_organization_model(), for_concrete_model=False)


#: Request-scoped memo for :func:`organization_ids_for_scope_ids`
#: (``{scope_id: organization_id}``).
#:
#: A contextvar rather than a module-level dict for the reason
#: ``vinta_billing.entitlement_cache`` gives for its own: a module-level cache
#: would leak between concurrently-handled requests, and -- worse here -- across
#: tests, where a rolled-back transaction hands the next test the same primary
#: keys pointing at different organizations.
_scope_translation_cache: contextvars.ContextVar[dict[int, int] | None] = contextvars.ContextVar(
    "scope_translation_cache", default=None
)

#: Where :func:`scope_for` memoizes its answer, on the organization instance.
#:
#: A second memo next to the contextvar above, and deliberately so -- they key
#: on different things. This one needs an ``Organization`` in hand and answers
#: "which scope bills it"; the contextvar has only a scope *id* and answers the
#: reverse, so there is nothing to hang it on. Merging them was tried and
#: reverted: the contextvar is only entered by
#: ``payments.middlewares.BillingScopeTranslationCacheMiddleware``, so dropping
#: this one costs two extra queries per service operation in every non-request
#: caller -- Celery tasks, management commands -- and three query-count gates
#: caught it.
#:
#: **Its lifetime is the instance's, which is not the transaction's.** An
#: ``Organization`` held across a rollback -- a broadly-scoped test fixture, a
#: worker reusing a model between tasks -- keeps a scope whose row is gone. The
#: instances this sees are per-request or per-task and do not outlive their
#: transaction, which is what makes it safe here; a caller that stashes an
#: organization somewhere longer-lived should re-fetch it rather than trust
#: this.
_SCOPE_CACHE_ATTR = "_billing_scope_cache"


@contextlib.contextmanager
def scope_translation_cache():
    """Memoize scope/organization translation for the duration of the block.

    Worth having because the engine asks each registered resource's counter for
    its own breakdown, and every one of them translates the *same* pooled scope
    ids back into organization ids. Unmemoized that is one query per resource --
    eight on ``GET /billing/usage/`` -- which is exactly the fan-out
    ``payments/tests/views/test_usage_view.py``'s query-count oracle exists to
    catch.

    Safe to memoize because the mapping cannot change inside a request: a
    scope's ``content_type`` and ``object_id`` are set when it is created and
    nothing updates them, so a scope cannot come to name a different
    organization.

    Re-entrant, so a nested block reuses the outer cache rather than shadowing
    it. Outside any block the translation still runs, just once per call -- the
    memo is an optimization, never a correctness requirement.
    """
    if _scope_translation_cache.get() is not None:
        yield
        return
    token = _scope_translation_cache.set({})
    try:
        yield
    finally:
        _scope_translation_cache.reset(token)


def scope_for(organization: Organization):
    """The scope that bills ``organization``, creating it if it is not there yet.

    The workhorse of the 0.8 upgrade: everywhere a billing service used to be
    handed an organization it is handed ``scope_for(organization)`` instead.

    **A plain lookup, not a reconcile.** This is on the hot path -- every limit
    check, every entitlement gate, every restriction guard goes through it, some
    of them per row -- so it does the cheapest thing that can be correct: read
    the cached answer, else one indexed ``SELECT``, and only fall through to
    :func:`sync_scope_for_organization` when there is genuinely no scope yet.
    Calling the full sync here instead (as the first cut of this module did)
    would walk the whole parent chain and re-check the mirrored fields on every
    call, turning one billing question into several queries. Keeping the mirror
    current is ``post_save``'s job, and it has already run.

    Memoized on the instance -- see :data:`_SCOPE_CACHE_ATTR` for why that is a
    separate memo from the contextvar the counters use, and for the lifetime
    constraint that comes with it.

    Creates on miss rather than returning ``None``. A caller reaching this has
    an organization in hand and is about to ask a billing question about it, and
    the only reason a scope would be missing is an organization written before
    this module existed or by a path that bypassed ``post_save`` (a
    ``bulk_create``, or a fixture loaded with signals muted). Answering the
    billing question is more useful than raising, and the row is the same one
    the backfill would have made.

    Not used to resolve a *request* -- that is ``SCOPE_RESOLVER``'s job, and it
    deliberately never writes. See ``vinta_billing.contrib.orgs.resolve_scope_from_organization``.
    """
    cached = getattr(organization, _SCOPE_CACHE_ATTR, None)
    if cached is not None:
        return cached

    scope = get_scope_model().objects.scope_for(organization)
    if scope is None:
        scope = sync_scope_for_organization(organization)

    setattr(organization, _SCOPE_CACHE_ATTR, scope)
    return scope


def sync_scope_for_organization(organization: Organization):
    """Create the organization's scope, and bring ``parent`` and the flag up to date.

    Idempotent, and safe to call on every save: it writes only when something it
    mirrors actually differs, so the common no-op save costs one select on the
    scope table and nothing else.

    The parent is resolved recursively -- an organization saved before its
    ancestors have scopes still gets a correctly linked chain, because reaching
    for the parent's scope provisions that one too.

    ``_provision`` below guards that recursion against a parent cycle. ``Organization.parent``
    is user-mutable through the Django admin, so ``a -> b -> a`` is reachable in
    practice, and this runs from ``post_save`` -- the save that *closes* the
    cycle is what would trigger it. Unguarded that is a ``RecursionError`` from
    inside a signal handler, which surfaces as a failed save rather than as the
    ``BillingRootCycleError`` the hierarchy raises for the same shape. Stopping
    the walk leaves the scope chain mirroring the organization chain, cycle
    included, which is what lets ``resolve_billing_root`` detect and report it.
    """
    return _provision(organization, set())


def _provision(organization: "Organization", seen: set[int]):
    """One link of the chain, with its ancestors provisioned first.

    Split out so the cycle-tracking set stays an implementation detail rather
    than a private parameter on a function five other modules call.
    """
    scope_model = get_scope_model()

    if organization.pk in seen:
        # Already provisioned on this walk. Return its scope without recursing
        # again -- the parent link was set when it was first visited.
        return scope_model.objects.scope_for(organization)
    seen.add(organization.pk)

    parent_scope = None
    # ``organization.parent`` rather than a filter on the id: the recursion is
    # what guarantees the whole chain exists, not just this one link. Read into
    # a local first -- the FK is nullable, so ``parent_id is not None`` alone
    # does not narrow the descriptor's type for the checker.
    parent = organization.parent
    if parent is not None:
        parent_scope = _provision(parent, seen)

    scope, created = scope_model.objects.get_or_create_for(
        organization,
        label=organization.name,
        parent=parent_scope,
    )

    # ``get_or_create_for`` returns an existing scope *untouched* -- deliberately,
    # so a second call cannot overwrite a curated label. That is the right
    # default for the package and the wrong one for the two fields this project
    # mirrors, so they are reconciled here.
    desired_meta = bool(organization.can_invite_organizations)
    updates: dict[str, object] = {}
    if scope.parent_id != (parent_scope.pk if parent_scope else None):
        updates["parent"] = parent_scope
    # Compared against the stored key rather than its truthiness, so a scope
    # whose `meta` never carried the key at all (everything `get_or_create_for`
    # and `vinta_billing`'s 0005 backfill create) gets it stamped on first sync
    # rather than being left implicit. `ResellerHierarchy.billing_root_q` no
    # longer depends on that -- see its `has_key` conjunct -- but an explicit
    # `false` is what makes the row readable in a shell.
    if (scope.meta or {}).get(RESELLER_ROOT_META_KEY) != desired_meta:
        meta = dict(scope.meta or {})
        meta[RESELLER_ROOT_META_KEY] = desired_meta
        updates["meta"] = meta
    if not created and scope.label != organization.name:
        updates["label"] = organization.name

    if updates:
        for field, value in updates.items():
            setattr(scope, field, value)
        scope.save(update_fields=[*updates, "modified"])

    # Refresh rather than leave `scope_for`'s memo pointing at a pre-update copy.
    setattr(organization, _SCOPE_CACHE_ATTR, scope)
    return scope


@receiver(
    post_save,
    # A string rather than the class: ``ModelSignal`` resolves it lazily through
    # the app registry, which is what lets this module stay out of the import
    # cycle described on ``_organization_model``.
    sender="organizations.Organization",
    dispatch_uid="payments_sync_billing_scope",
)
def _sync_scope_on_organization_save(sender, instance, **kwargs) -> None:
    """Keep the scope mirror in step with every organization write.

    Fires on update as well as create, because ``parent`` and
    ``can_invite_organizations`` are both mutable: promoting an organization to
    a reseller has to move it to being its own billing root, and re-parenting
    one has to move which ceiling its usage pools into. Both are the kind of
    change that is made once and relied on for years, so drifting here is worse
    than the cost of the check.
    """
    sync_scope_for_organization(instance)


def organization_for(scope) -> "Organization | None":
    """The organization a scope bills, or ``None`` if it names something else.

    The inverse of :func:`scope_for`, for the handful of places holding a scope
    that need the organization back -- a billing object's ``scope`` traversed to
    reach organization-scoped rows, most often. Reads the generic key rather
    than a foreign key, which is why the answer is optional: nothing in the
    shipped scope model constrains it to name an organization.
    """
    if scope is None:
        return None
    object_id = getattr(scope, "object_id", None)
    if not object_id:
        return None
    return _organization_model()._default_manager.filter(pk=object_id).first()


def scopes_for(organizations: Iterable["Organization"]) -> tuple[list[Any], dict[int, int]]:
    """Scopes for many organizations at once, plus the way back.

    Returns ``(scopes, {scope_pk: organization_pk})``. The bulk counterpart to
    :func:`scope_for`, for the batch entitlement paths whose whole reason for
    existing is answering in two queries rather than two per row -- calling
    :func:`scope_for` in a loop there would reintroduce exactly the N+1 they
    avoid.

    Unlike :func:`scope_for` this does *not* create missing scopes. A batch read
    is the wrong place to provision: it is invoked from list endpoints, where a
    write per un-provisioned row would be a surprise, and any organization
    reaching one has been saved at least once and so already has a scope from
    ``post_save``. An organization without one is simply absent from both
    halves of the result, which the callers already treat as "not entitled".
    """
    organizations = list(organizations)
    if not organizations:
        return [], {}

    scope_model = get_scope_model()
    by_object_id = {str(organization.pk): organization.pk for organization in organizations}
    scopes = list(
        scope_model.objects.filter(
            content_type=_organization_content_type(), object_id__in=list(by_object_id)
        )
    )
    scope_to_organization = {
        scope.pk: by_object_id[scope.object_id]
        for scope in scopes
        if scope.object_id in by_object_id
    }
    return scopes, scope_to_organization


def organization_ids_for_scope_ids(scope_ids: Iterable[int]) -> dict[int, int]:
    """``{scope_id: organization_id}`` -- the inverse, for counter input.

    ``object_id`` is a ``CharField`` on the shipped scope (it holds the primary
    key of whatever kind of thing the scope names, which need not be an
    integer), so it is cast back here. Every scope this project makes names an
    organization, but a row that somehow does not is skipped rather than
    crashing the count.
    """
    ids = list(scope_ids)
    if not ids:
        return {}

    memo = _scope_translation_cache.get()
    if memo is not None:
        known = {sid: memo[sid] for sid in ids if sid in memo}
        if len(known) == len(ids):
            return known
        ids = [sid for sid in ids if sid not in memo]
    else:
        known = {}

    scope_model = get_scope_model()
    content_type = _organization_content_type()
    mapping: dict[int, int] = dict(known)
    for scope_id, object_id in scope_model.objects.filter(
        pk__in=ids, content_type=content_type
    ).values_list("pk", "object_id"):
        try:
            organization_id = int(object_id)
        except (TypeError, ValueError):
            continue
        mapping[scope_id] = organization_id
        if memo is not None:
            memo[scope_id] = organization_id
    return mapping

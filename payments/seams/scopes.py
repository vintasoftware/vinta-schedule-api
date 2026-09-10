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
3. :func:`organization_ids_for_scope_ids` / :func:`scope_ids_for_organization_ids`
   -- the id-space translation the usage counters in ``payments.seams.resources``
   need, because the engine speaks scope ids and this project's own tables are
   keyed by organization id.

Why a signal rather than a call in ``OrganizationService.create_organization``:
there are four organization-creation paths (see
``organizations/tests/test_organization_creation_billing.py`` for the list --
the REST funnel, the reseller GraphQL mutation's raw ``objects.create``, the
admin, and tenant provisioning), only one of which goes through the service. A
scope that a reseller-created child never gets is not a loud failure; it is a
child that silently bills nowhere. ``post_save`` is the one hook all four share.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def scope_for(organization: Organization):
    """The scope that bills ``organization``, creating it if it is not there yet.

    The workhorse of the 0.8 upgrade: everywhere a billing service used to be
    handed an organization it is handed ``scope_for(organization)`` instead.

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
    return sync_scope_for_organization(organization)


def sync_scope_for_organization(organization: Organization):
    """Create the organization's scope, and bring ``parent`` and the flag up to date.

    Idempotent, and safe to call on every save: it writes only when something it
    mirrors actually differs, so the common no-op save costs one select on the
    scope table and nothing else.

    The parent is resolved recursively -- an organization saved before its
    ancestors have scopes still gets a correctly linked chain, because reaching
    for the parent's scope provisions that one too.
    """
    scope_model = get_scope_model()

    parent_scope = None
    if organization.parent_id is not None:
        # ``organization.parent`` rather than a filter on the id: the recursion
        # is what guarantees the whole chain exists, not just this one link.
        parent_scope = sync_scope_for_organization(organization.parent)

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
    if bool((scope.meta or {}).get(RESELLER_ROOT_META_KEY)) != desired_meta:
        meta = dict(scope.meta or {})
        meta[RESELLER_ROOT_META_KEY] = desired_meta
        updates["meta"] = meta
    if not created and scope.label != organization.name:
        updates["label"] = organization.name

    if updates:
        for field, value in updates.items():
            setattr(scope, field, value)
        scope.save(update_fields=[*updates, "modified"])

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


def scope_ids_for_organization_ids(organization_ids: Iterable[int]) -> dict[int, int]:
    """``{organization_id: scope_id}`` for the organizations named.

    Organizations with no scope are absent rather than mapped to ``None`` --
    same contract as the counters' own breakdowns, so a caller never has to
    strip empties.
    """
    scope_model = get_scope_model()
    ids = [str(pk) for pk in organization_ids]
    if not ids:
        return {}
    content_type = _organization_content_type()
    return {
        int(object_id): scope_id
        for object_id, scope_id in scope_model.objects.filter(
            content_type=content_type, object_id__in=ids
        ).values_list("object_id", "pk")
    }


def organization_ids_for_scope_ids(scope_ids: Iterable[int]) -> dict[int, int]:
    """``{scope_id: organization_id}`` -- the inverse, for counter input.

    ``object_id`` is a ``CharField`` on the shipped scope (it holds the primary
    key of whatever kind of thing the scope names, which need not be an
    integer), so it is cast back here. Every scope this project makes names an
    organization, but a row that somehow does not is skipped rather than
    crashing the count.
    """
    scope_model = get_scope_model()
    ids = list(scope_ids)
    if not ids:
        return {}
    content_type = _organization_content_type()
    mapping: dict[int, int] = {}
    for scope_id, object_id in scope_model.objects.filter(
        pk__in=ids, content_type=content_type
    ).values_list("pk", "object_id"):
        try:
            mapping[scope_id] = int(object_id)
        except (TypeError, ValueError):
            continue
    return mapping


def rekey_breakdown_to_scopes(
    breakdown: Mapping[int, int], organization_to_scope: Mapping[int, int]
) -> dict[int, int]:
    """Turn a ``{organization_id: count}`` breakdown into ``{scope_id: count}``.

    What every counter in ``payments.seams.resources`` returns through. The
    engine indexes usage by scope; this project's tables group by organization.
    An organization with no scope drops out of the result -- it has no ceiling
    to count against, so attributing its rows to anything would be a guess.
    """
    rekeyed: dict[int, int] = {}
    for organization_id, count in breakdown.items():
        scope_id = organization_to_scope.get(organization_id)
        if scope_id is not None:
            rekeyed[scope_id] = rekeyed.get(scope_id, 0) + count
    return rekeyed

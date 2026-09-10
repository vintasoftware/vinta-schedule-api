"""The host's reseller hierarchy, expressed in ``vinta_billing.hierarchy``'s
vocabulary.

``organizations.Organization`` is self-referential (``parent``) and carries a
``can_invite_organizations`` flag: an organization with no parent is a billing
root, and so is a child that flag marks as its own reseller -- it pays for its
own subtree rather than pooling into a grandparent's ceiling.

Up to 0.7 this seam was four lines: ``ParentFieldHierarchy`` walked
``Organization`` directly, so naming its two fields was the whole job. 0.8.0
moved every billing row onto a ``BillingScope``, and the hierarchy now walks
*scopes*. The shape is the same -- ``AbstractBillingScope`` ships its own
self-referential ``parent`` -- but two things do not carry across on their own,
and this module is where both are handled.

**The parent chain has to be mirrored.** ``vinta_billing``'s
``0005_backfill_scopes`` creates one scope per organization and re-points every
billing row at it, but it leaves ``parent`` NULL: it has no way to know that
this project's organizations form a tree. Left alone the reseller chain
flattens, every child becomes its own billing root, and a reseller's children
stop pooling into its ceiling -- which is a silent under-count of usage against
the paying root, not an error anyone would see.
``payments/migrations/0026_mirror_organization_tree_onto_scopes.py`` backfills
it, and ``payments.seams.scopes`` keeps it in step from then on.

**The reseller flag lives in ``meta``, not in a column.** ``can_invite_organizations``
is a field on ``Organization``, and the scope has nothing like it. The usual
answer would be a project-owned scope model -- ``BILLING_SCOPE_MODEL`` is
swappable for exactly that, and ``audit_integration.OrganizationAuditScope``
is this project's precedent for it -- but that path is closed on an *upgrade*
in 0.8.0: ``0003_billingscope`` creates the shipped model with
``options={"swappable": "BILLING_SCOPE_MODEL"}``, so Django skips the table
when the setting points elsewhere, while ``0005_backfill_scopes`` hardcodes
``apps.get_model("vinta_billing", "BillingScope")`` and writes to it
regardless. Pointing the setting at our own model makes the backfill write to
a table that was never created. So the shipped scope stays, and the flag goes
into the ``meta`` JSON every ``BaseModel`` in that package already carries.

That costs an unindexed JSON read on ``billing_root_q()``. It is a filter over
the scope table -- one row per organization, not per event -- so the scan is
cheap at this project's size. Revisit if either the upstream migration learns
to respect the swappable setting (at which point a real column and a proper
scope model are the better answer) or the scope table grows past the point
where a sequential scan on it stops being free.
"""

from __future__ import annotations

from django.db.models import Model, Q

from vinta_billing.hierarchy import ParentFieldHierarchy


#: The ``meta`` key mirroring ``Organization.can_invite_organizations`` onto the
#: organization's scope. Named here rather than inlined because the migration
#: that backfills it and the sync seam that maintains it both need the same
#: string, and a typo in any one of the three is a silently wrong billing root.
RESELLER_ROOT_META_KEY = "is_reseller_root"


class ResellerHierarchy(ParentFieldHierarchy):
    """The scope parent chain, with the mirrored reseller flag marking new roots.

        ``parent_field`` stays the inherited ``"parent"`` -- the scope's own. Only
        the flag needs overriding, and it needs overriding in both directions:
        ``is_billing_root`` for a scope already in hand, and ``billing_root_q`` for
        the queries that select roots across many scopes at once.

    ``scope`` stays typed as ``Model`` to match the supertype, and both fields are
        read through ``getattr`` -- the same idiom ``ParentFieldHierarchy`` uses for
        its own configurable field names, and the reason it can: the scope model is
        resolved at runtime through ``get_scope_model()``, so there is no concrete
        class to annotate against here.

        ``root_flag_field`` is deliberately left ``None``. The base class would
        otherwise build ``Q(is_reseller_root=True)`` against a column that does not
        exist, and ``getattr(scope, "is_reseller_root")`` would raise -- both
        methods below replace that behaviour rather than extend it.
    """

    def is_billing_root(self, scope: Model) -> bool:
        """True at the top of the chain, or wherever the mirrored flag says so.

        ``meta`` is ``default=dict`` on the model, but a row written by a
        historical model in a migration can still hold SQL NULL, so the
        ``or {}`` is not redundant.
        """
        if getattr(scope, f"{self.parent_field}_id") is None:
            return True
        return bool((getattr(scope, "meta", None) or {}).get(RESELLER_ROOT_META_KEY))

    def billing_root_q(self) -> Q:
        """The queryset equivalent of :meth:`is_billing_root`.

        Must agree with it exactly. A scope this selects but that method
        rejects (or the reverse) puts a subscription and the usage pooling into
        it on two different roots.

        The ``has_key`` conjunct is load-bearing, not defensive. ``ParentFieldHierarchy
        .pooled_scope_ids`` uses this through ``.exclude(...)``, and a bare
        ``meta__is_reseller_root=True`` against a row whose ``meta`` does not
        carry the key at all evaluates to SQL NULL rather than FALSE -- so
        ``NOT (parent IS NULL OR NULL)`` is NULL, and Postgres drops the row.
        Every plain child would silently fall out of its root's pool: no error,
        just usage that stops counting against the ceiling it is charged to.

        Rows without the key are real. ``get_or_create_for`` writes ``meta`` as
        ``{}``, and ``vinta_billing``'s own ``0005`` backfill does the same, so
        this cannot rely on :func:`payments.seams.scopes.sync_scope_for_organization`
        having stamped every row first. ``has_key`` is the ``?`` operator and
        answers a real boolean, which makes the conjunction FALSE instead of
        NULL and the exclusion correct.
        """
        return Q(parent__isnull=True) | (
            Q(**{"meta__has_key": RESELLER_ROOT_META_KEY})
            & Q(**{f"meta__{RESELLER_ROOT_META_KEY}": True})
        )

"""This project's concrete billing scope.

``vinta-django-billing`` 0.8.0 moved every billing row off the organization and
onto a *scope* -- a row naming whoever pays -- so that one installation can sell
to an organization, a bare user, or anything else. It ships a scope model that
addresses its payer through a generic key (``content_type`` + ``object_id``),
which is the right default for a package that cannot know what a payer is here.

This project has exactly one kind of payer, and says so with a real foreign key.
``BILLING_SCOPE_MODEL`` points at this model, and the whole engine -- every
service signature, every FK, the hierarchy, the permission and recipient seams
-- resolves through it.

Why not keep the shipped model: with a generic key, "which organization does
this scope bill" is a string compare against a content type, so nothing joins.
That cost the project a translation layer in ``payments/seams/``, a
request-scoped memo, a middleware to activate it, and a reseller flag hidden in
a JSON blob because ``ParentFieldHierarchy`` needs a real column to filter on.
All of that was working around the absence of this foreign key.

Mirrors ``audit_integration.OrganizationAuditScope``, which is the same move for
``vinta-django-audit-logs`` and the reason this lives in an app of its own
rather than in ``payments``: a scope model must be creatable *before*
``vinta_billing``'s ``0004`` adds the foreign keys that point at it, and
``payments``' migrations already depend on ``vinta_billing``. Putting it there
orders the foreign key ahead of its own table and a build from zero fails with
``Related model ... cannot be resolved``.
"""

from django.db import models

from vinta_billing.models import AbstractBillingScope


class OrganizationBillingScope(AbstractBillingScope):
    """The scope that bills one organization.

    ``organization`` is a ``OneToOneField``, not a nullable generic key: every
    payer here is an organization and exactly one scope bills each of them.

    ``CASCADE``, and the alternatives are worse. ``PROTECT`` reads better in
    isolation -- billing history should not evaporate -- but every organization
    has a scope from its first save, so it would make every organization
    permanently undeletable, which is a product change this model has no
    business making (``calendar_integration/tests/test_attendance_protect_fk.py``
    deletes one). ``SET_NULL`` would need a nullable payer, and a billing scope
    that names nobody contradicts ``AbstractBillingScope.validate_scope``.

    So deleting an organization now deletes its scope, and the scope's own
    ``CASCADE`` relations take its billing rows with it. That is a real change
    from the shipped generic-key scope, which had no foreign key and so left
    both behind: rows pointing at a scope whose payer no longer existed. Losing
    them is the more honest of the two outcomes, but it *is* a loss -- an
    installation that needs billing history to outlive its organizations should
    stop hard-deleting them rather than reach for ``PROTECT`` here.

    ``is_reseller_root`` mirrors ``Organization.can_invite_organizations``, and
    ``parent`` (inherited) mirrors ``Organization.parent``. Both are real
    columns so ``vinta_billing.hierarchy.ParentFieldHierarchy`` can walk and
    filter on them directly -- naming the two field names is the whole of
    ``payments.seams.hierarchy.ResellerHierarchy`` again. They are a mirror
    rather than the source of truth, kept in step by
    ``payments.seams.scopes``; the organization tree remains the thing a human
    edits.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_scope",
    )

    #: ``validate_scope`` wants a CHECK constraint saying a scope names
    #: something. Here that is structural: ``organization`` is a non-nullable
    #: foreign key, so the column cannot be empty.
    #:
    #: Mirrors ``Organization.can_invite_organizations``. A flagged scope is its
    #: own billing root: it pays for its own subtree rather than pooling into a
    #: grandparent's ceiling.
    is_reseller_root = models.BooleanField(default=False, db_index=True)

    @property
    def scope(self):
        """The thing being billed. Always an organization here."""
        return self.organization

    @scope.setter
    def scope(self, value) -> None:
        self.organization = value

    def build_scope_key(self) -> str:
        """``"organizations.organization:<pk>"`` -- the same string the shipped
        model built from its generic key.

        Deliberately byte-identical to what ``vinta_billing``'s ``BillingScope``
        produced, because ``billing_integration/0002`` copies ``scope_key``
        across verbatim when adopting an existing installation's scopes. A
        different format here would mean the first ordinary ``save()`` after
        that migration silently rewrote the key -- and the key is what
        ``AbstractBillingScope`` promises is stable for the life of the scope.

        The label comes off the foreign key rather than being written out, so it
        follows ``ORGANIZATION_MODEL`` if that is ever pointed elsewhere.
        """
        if self.organization_id is None:
            return ""
        # `related_model` is `Optional` only for a generic relation; this field
        # is a concrete foreign key, so it always resolves.
        related = self._meta.get_field("organization").related_model
        assert related is not None  # noqa: S101
        return f"{related._meta.label_lower}:{self.organization_id}"

    def __str__(self) -> str:
        return self.label or self.build_scope_key() or str(self.pk)

"""The host's reseller hierarchy, expressed in ``vinta_billing.hierarchy``'s
vocabulary.

``organizations.Organization`` is self-referential (``parent``) and carries a
``can_invite_organizations`` flag: an organization with no parent is a billing
root, and so is a child that flag marks as its own reseller -- it pays for its
own subtree rather than pooling into a grandparent's ceiling. That is exactly
the shape ``vinta_billing.hierarchy.ParentFieldHierarchy`` already implements
against configurable field names, so this seam only has to name them.

``payments.services.subscription_service.is_billing_root`` /
``resolve_billing_root`` compute this same predicate and walk by hand today;
this class is their generic equivalent, wired in through
``VINTA_BILLING['HIERARCHY']``.
"""

from __future__ import annotations

from vinta_billing.hierarchy import ParentFieldHierarchy


class ResellerHierarchy(ParentFieldHierarchy):
    """``organizations.Organization``'s parent chain and reseller flag."""

    parent_field = "parent"
    root_flag_field = "can_invite_organizations"

"""The host's reseller hierarchy, expressed in ``vinta_billing.hierarchy``'s
vocabulary.

``billing_integration.OrganizationBillingScope`` is self-referential
(``parent``) and carries an ``is_reseller_root`` flag: a scope with no parent is
a billing root, and so is one that flag marks as its own reseller -- it pays for
its own subtree rather than pooling into a grandparent's ceiling. Both mirror
the organization tree, kept in step by ``payments.seams.scopes``.

That is exactly the shape ``ParentFieldHierarchy`` already implements against
configurable field names, so this seam only has to name them.

It briefly did more. While ``BILLING_SCOPE_MODEL`` still pointed at
``vinta_billing``'s shipped scope there was no column to put the reseller flag
in, so it lived in a JSON blob and this class overrode both
:meth:`is_billing_root` and :meth:`billing_root_q` to read it -- the second with
a ``has_key`` conjunct, because ``exclude()`` over a missing JSON key evaluates
to SQL NULL and silently dropped every plain child out of its root's pool. Real
columns retired all of it.
"""

from __future__ import annotations

from vinta_billing.hierarchy import ParentFieldHierarchy


class ResellerHierarchy(ParentFieldHierarchy):
    """``OrganizationBillingScope``'s parent chain and reseller flag."""

    parent_field = "parent"
    root_flag_field = "is_reseller_root"

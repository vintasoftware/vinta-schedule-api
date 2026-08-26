"""Project-specific audit filters.

``vinta_audit_logs.AuditQuery`` is deliberately narrow. Every field on it is a
portable value -- a string, a timestamp, a pair of strings -- because the same
query object has to mean the same thing pointed at a Postgres table, an
in-memory dict, or a warehouse. Nothing on it can name a column that only exists
because *this* project swapped in its own scope and identity models.

Which leaves a real question unanswered: how do you ask for "every record whose
actor is the membership belonging to hugo@vinta.com.br"? The email lives on
``users.User``, two joins away, and no portable query object can know that.

The answer is to extend the query object and the repository together, which is
what this module and ``OrganizationAuditRepository._filtered_queryset`` do. The
rules that make it safe:

* **Extend, never replace.** ``OrganizationAuditQuery`` subclasses ``AuditQuery``,
  so every portable filter still works and any repository can still be handed
  one -- it will simply ignore the extra fields.
* **The extra fields are optional and default to None**, so an
  ``OrganizationAuditQuery`` with none of them set behaves exactly like the
  ``AuditQuery`` it inherits from.
* **A backend that cannot honour a field must not silently ignore it.** Handing
  one of these to a repository that filters in Python would otherwise return
  results that look right and are not. ``AuditQuery.active_extension_fields``
  makes that detectable, and ``vinta_audit_logs.filtering.record_matches``
  raises rather than guessing.

Cost
----
These filters join. That is the whole point of them -- they reach columns the
audit row does not carry -- but it means they do not use the browse indexes that
the portable filters were designed around, and they get slower as the log grows.
They are for investigation ("who did this, and who are they"), not for the
paginated audit view a user loads on every page.

Pair them with a portable filter whenever you can. ``scope_keys`` plus an actor
email lets Postgres cut the log down to one tenant on the index first and join
only what survives; the email on its own is a scan.
"""

from __future__ import annotations

from dataclasses import dataclass

from vinta_audit_logs.types import AuditQuery


@dataclass(frozen=True)
class OrganizationAuditQuery(AuditQuery):
    """An ``AuditQuery`` that can also reach into this project's own models.

    Example -- everything one person did in one organization::

        service.query(
            OrganizationAuditQuery(
                scope_keys=[str(organization.id)],
                actor_user_emails=["hugo@vinta.com.br"],
            )
        )

    Every field follows the same rules as the base class: ``None`` means the
    filter is not active, and an empty list is an active filter that nothing
    satisfies.
    """

    # actor.user.email IN (...) -- case-insensitive, because email is.
    # Two joins: audit -> identity -> user.
    actor_user_emails: list[str] | None = None

    # The audit identity's live group relation. "every action taken by someone
    # who was in this group at the time", which the ``group_names`` snapshot
    # answers too -- but this one survives a rename, where the snapshot holds the
    # name as it was.
    actor_group_ids: list[int] | None = None

    # actor.membership_permissions -- "every action taken by someone who held
    # this permission at the time". The question an incident review asks.
    actor_permission_codenames: list[str] | None = None

    # The organization behind the scope, rather than its key. Equivalent to
    # ``scope_keys=[str(pk)]`` and cheaper to write when a caller holds the row;
    # unlike the email filters this one still lands on the scope_key index.
    organization_ids: list[int] | None = None

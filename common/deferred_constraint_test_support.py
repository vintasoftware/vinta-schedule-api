"""Checking ``DEFERRABLE INITIALLY DEFERRED`` constraints without a real ``COMMIT``.

The composite membership FKs this project enforces in raw SQL -- ``EventAttendance``,
``CalendarOwnership``, ``CalendarManagementToken`` -- are all
``NO ACTION DEFERRABLE INITIALLY DEFERRED``, so PostgreSQL postpones their check to the
end of the transaction. That is deliberate: an ``Organization`` delete cascades to the
membership and to the rows referencing it in one transaction, and a per-statement check
would abort that cascade depending on the order the collector happened to pick.

Testing a deferred constraint therefore needs a point where the check actually runs.
Reaching it via a real ``COMMIT`` means ``@pytest.mark.django_db(transaction=True)``,
which costs a full-database ``flush`` on teardown -- about 0.25s per test, and there are
~25 such tests. ``SET CONSTRAINTS ALL IMMEDIATE`` reaches the same point from inside a
transaction: PostgreSQL checks every deferred constraint right there and raises the same
``IntegrityError`` the ``COMMIT`` would have raised, so the tests keep their meaning and
run inside the ordinary rolled-back test transaction.

This is the mechanism Django's own ``TestCase`` uses for the same reason -- see
``django.test.TestCase._fixture_teardown`` calling ``connection.check_constraints()``,
which exists precisely because a rolled-back test would otherwise never reach the check.

A test converted this way is only meaningful while the call below is present: without
it the deferred check never runs and a ``pytest.raises(IntegrityError)`` block fails with
``DID NOT RAISE`` rather than passing vacuously. That failure mode is the reason the call
is explicit at each site instead of hidden in a fixture -- a reader can see that the
assertion has a point where it is made.
"""

from django.db import connection


def check_deferred_constraints_now() -> None:
    """Force every deferred constraint to be checked at this point in the transaction.

    Raises whatever the eventual ``COMMIT`` would have raised -- ``IntegrityError`` for a
    violated FK -- and is a no-op when nothing is violated.

    Applies to the rest of the current transaction: once set to ``IMMEDIATE`` the
    constraints stay immediate until it ends. Harmless here, because each test runs in
    its own transaction, but it does mean a later statement in the *same* test raises at
    the statement rather than at this call.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

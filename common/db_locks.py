"""Postgres advisory locks for work that must not run twice at once."""

import hashlib

from django.db import connection


def advisory_lock_key(name: str) -> int:
    """The signed 64-bit integer Postgres keys the advisory lock ``name`` by."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def try_advisory_xact_lock(name: str) -> bool:
    """Take the transaction-level advisory lock ``name`` if no one else holds it.

    Returns ``False`` at once, without waiting, when another transaction holds it.
    The lock is released when the current transaction ends, so this must run inside
    ``transaction.atomic()``. It locks a name, not a row: use it to serialize work
    that has no row to lock yet, such as creating the one row a unique key allows.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [advisory_lock_key(name)])
        row = cursor.fetchone()
    return bool(row and row[0])

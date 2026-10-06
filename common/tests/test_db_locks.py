from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction
from django.test import TestCase

import psycopg

from common.db_locks import advisory_lock_key, try_advisory_xact_lock


@contextmanager
def _held_by_another_connection(name: str) -> Iterator[None]:
    """Hold the advisory lock ``name`` in an open transaction on a second connection."""
    with psycopg.connect(**connection.get_connection_params()) as other:
        with other.transaction():
            row = other.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", [advisory_lock_key(name)]
            ).fetchone()
            assert row == (True,)
            yield


class TryAdvisoryXactLockTest(TestCase):
    def test_a_free_lock_is_taken(self) -> None:
        with transaction.atomic():
            assert try_advisory_xact_lock("db-locks-test:free") is True

    def test_a_lock_held_by_another_transaction_is_refused_without_waiting(self) -> None:
        with _held_by_another_connection("db-locks-test:held"), transaction.atomic():
            assert try_advisory_xact_lock("db-locks-test:held") is False
            assert try_advisory_xact_lock("db-locks-test:other-name") is True

    def test_the_lock_is_free_again_once_the_holder_ends(self) -> None:
        with _held_by_another_connection("db-locks-test:released"):
            pass

        with transaction.atomic():
            assert try_advisory_xact_lock("db-locks-test:released") is True

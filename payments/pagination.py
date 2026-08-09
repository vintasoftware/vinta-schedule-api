from rest_framework.pagination import LimitOffsetPagination


class LargeLimitOffsetPagination(LimitOffsetPagination):
    """``LimitOffsetPagination`` with a raised ceiling for audit-style pulls.

    Used by ``GET /billing/usage/occurrences/`` (the metered-occurrence
    ledger): a customer disputing an invoice may need to page through an
    entire cycle's worth of line items, and a whole cycle for an active
    organization can run into the thousands. The project default page size
    (``PAGE_SIZE = 10``) still applies when ``limit`` is omitted -- this only
    raises how large a single explicit ``limit`` may request, so one audit
    pull does not need a thousand round trips while any single response stays
    bounded.
    """

    max_limit = 1000

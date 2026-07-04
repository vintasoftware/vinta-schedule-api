from django.db import models


class PolicyDocumentQuerySet(models.QuerySet):
    """Chainable queryset for PolicyDocument."""

    def of_type(self, document_type: str) -> "PolicyDocumentQuerySet":
        """Filter to rows of a single document_type."""
        return self.filter(document_type=document_type)

    def latest_per_type(self) -> "PolicyDocumentQuerySet":
        """Return one row per document_type: the highest-version row.

        Implemented with Postgres ``DISTINCT ON`` via ``QuerySet.distinct(*fields)``
        (Postgres-only API). The project's sole supported database is Postgres, so
        this avoids a less efficient group-by-then-fetch round trip.
        """
        return self.order_by("document_type", "-version").distinct("document_type")

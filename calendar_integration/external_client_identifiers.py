"""Target registry and normalization for ``ExternalClientIdentifier``.

``ExternalClientIdentifier`` uses a generic foreign key so it can point at more than
one model, but the *write surface* only accepts the models listed in
``IDENTIFIABLE_MODELS`` -- an open generic-FK write path reachable by a public API
token would let a caller key rows to arbitrary tables.
"""

from urllib.parse import urlsplit, urlunsplit


#: Models an ExternalClientIdentifier may point at, as "app_label.modelname" (lowercased,
#: matching ContentType.model). The table is generic; the write surface is not -- an open
#: generic-FK write path reachable by a public API token would let a caller key rows to
#: arbitrary tables.
IDENTIFIABLE_MODELS: frozenset[str] = frozenset(
    {
        "calendar_integration.calendarevent",
        "calendar_integration.externalattendee",
    }
)


def normalize_system(value: str) -> str:
    """Lowercase scheme and host, strip a trailing slash from the path.

    Uniqueness is keyed on ``system``, so two spellings of one host must not become two
    systems. Every write path calls this -- the service, the DRF serializer and the admin
    form -- because bulk_create bypasses ``Model.save()``.
    """
    scheme, netloc, path, query, fragment = urlsplit(value)

    normalized_path = path[:-1] if path.endswith("/") else path

    return urlunsplit(
        (scheme.lower(), netloc.lower(), normalized_path, query, fragment),
    )

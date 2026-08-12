"""Wire-level constants shared across apps.

Kept here rather than in the module that happens to read them first so the
``X-Organization-Id`` contract has exactly one definition: ``common.utils.
view_utils`` (request resolution), ``common.openapi`` (the OpenAPI parameter,
and therefore ``schema.yml``) and ``common.org_retrievers`` (the
``vinta-django-orgs`` retriever) all read the same name, so the header the
schema documents cannot drift from the header the code looks for.
"""

#: Header a client sends to select the active organization for a request.
ACTIVE_ORG_HEADER = "X-Organization-Id"

#: The same header as Django exposes it on ``request.META``. Spelled out rather
#: than derived at every call site, which is how the two spellings drift apart.
ACTIVE_ORG_HEADER_META_KEY = "HTTP_" + ACTIVE_ORG_HEADER.replace("-", "_").upper()

"""Framework-agnostic shared constants.

Deliberately DRF-free (no ``rest_framework`` import anywhere in this module,
transitively or otherwise) so it can be imported from plain Django
request-handling code -- e.g. ``common/org_retrievers.py``, which must stay
importable with no DRF in its import chain -- as well as from DRF-specific
code such as ``common/utils/view_utils.py`` and ``common/openapi.py``.
"""

from __future__ import annotations


#: Header name used to select the active organization for a request. The
#: single source of truth: ``common.utils.view_utils.ACTIVE_ORG_HEADER`` and
#: ``common.org_retrievers.ORGANIZATION_ID_HEADER`` both alias this constant
#: rather than redeclaring the literal, so ``common.openapi``'s published
#: OpenAPI schema parameter name and the retriever's header lookup can never
#: drift apart.
ACTIVE_ORG_HEADER = "X-Organization-Id"

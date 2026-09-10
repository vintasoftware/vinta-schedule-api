"""Request-lifetime plumbing for the billing seams.

One middleware, and it exists for one reason: to give
:func:`payments.seams.scopes.organization_ids_for_scope_ids` a place to memoize
for the length of a request.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from payments.seams.scopes import scope_translation_cache


class BillingScopeTranslationCacheMiddleware:
    """Activate the scope-to-organization memo for the whole request.

    ``vinta-django-billing`` 0.8.0 keys usage on the scope; this project's own
    tables are keyed by ``organization_id``. Every registered resource's counter
    therefore translates the same pooled scope ids back into organization ids,
    and unmemoized that is one query per resource -- eight on
    ``GET /billing/usage/``. The memo collapses them to one.

    Note what this is *not* about. Pointing ``BILLING_SCOPE_MODEL`` at
    ``billing_integration.OrganizationBillingScope`` gave scopes a real
    ``organization`` foreign key, which retired the rest of the translation
    scaffolding -- ``scope_for`` is a reverse one-to-one Django caches per
    instance, and the reseller flag is a column the hierarchy filters on
    directly. It did not retire this: the counters read organization-keyed
    tables and the engine hands them scope ids, so somebody still has to map
    between the two, once per counter. Deleting this middleware took the
    usage endpoint from 23 queries to 29, which is how it earned its way back.

    **Middleware rather than a view override.** The natural place looked like a
    ``dispatch`` override on ``VINTA_BILLING['VIEW_MIXIN']``, and that is wrong:
    ``vinta_orgs``' ``OrganizationScopedAPIViewMixin`` owns ``dispatch`` and
    ``perform_authentication`` as its tenancy-resolution seam, and
    ``common/tests/test_tenant_scoped_mro.py`` pins that every routed view
    resolves both to the package. Wrapping the request from outside leaves that
    seam alone, and it also covers the paths a billing view mixin never
    sees -- ordinary writes that call ``check_limit``, and the public GraphQL
    API's entitlement gate.

    Placed early and wrapping everything below it, for the same reason
    ``entitlement_request_cache`` is activated around the public-API request in
    ``public_api.middlewares``: the memo has to be live before any view code
    asks a billing question.

    Correctness does not depend on it. Outside the block the translation still
    runs, just once per call instead of once per request -- see
    :func:`~payments.seams.scopes.scope_translation_cache`.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        with scope_translation_cache():
            return self.get_response(request)

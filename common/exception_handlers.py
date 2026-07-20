"""Project-wide DRF exception handler.

Registered as ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``. It delegates to DRF's own
handler for everything it does not explicitly know about, so adding a case here
cannot change the *rendering* of any existing error.

It can, however, change **transactional semantics**, which is not the same claim.
Returning a ``Response`` swallows the exception, so under
``ATOMIC_REQUESTS = True`` (production) the request transaction would otherwise
*commit* everything written before the raise. Every branch that returns a
``Response`` here must therefore call ``rest_framework.views.set_rollback()``
first, exactly as DRF's own handler does for every ``APIException``.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback

from payments.exceptions import OverLimitError


def vinta_exception_handler(exc: Exception, context: dict) -> Response | None:
    """Render domain exceptions that are not DRF ``APIException`` subclasses.

    ``OverLimitError`` is rendered as **HTTP 402 Payment Required** rather than
    403, so a client can distinguish "you are not allowed to do this" from "you
    have run out of capacity, here is how to get more". The body is the shared
    over-limit contract (``OverLimitError.as_error_body``) — the GraphQL surface
    renders the same dict through its own error extension, so the two surfaces
    stay byte-identical without either restating the shape.
    """
    if isinstance(exc, OverLimitError):
        # Mandatory before returning a Response: swallowing the exception here
        # would otherwise commit the ATOMIC_REQUESTS transaction, persisting
        # whatever a guarded service wrote before it hit the limit check (an
        # invitation row, a membership reactivation, audit entries) while the
        # client is told 402.
        set_rollback()
        return Response(exc.as_error_body(), status=status.HTTP_402_PAYMENT_REQUIRED)
    return drf_exception_handler(exc, context)

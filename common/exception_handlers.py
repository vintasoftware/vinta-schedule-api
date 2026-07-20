"""Project-wide DRF exception handler.

Registered as ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``. It delegates to DRF's own
handler for everything it does not explicitly know about, so adding a case here
cannot change the rendering of any existing error.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

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
        return Response(exc.as_error_body(), status=status.HTTP_402_PAYMENT_REQUIRED)
    return drf_exception_handler(exc, context)

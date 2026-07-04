from http import HTTPStatus

from django.http import JsonResponse

from allauth.core.exceptions import ImmediateHttpResponse


class AccountError(Exception):
    """Base exception for account related errors"""

    pass


class UserNotAuthenticatedError(AccountError):
    def __init__(self, message="User must be authenticated to generate an access token."):
        super().__init__(message)


class ConsentRequiredError(AccountError, ImmediateHttpResponse):
    """Refuses an SMS send because the user has no recorded SMS-messaging consent.

    This is the server-side backstop behind the SMS consent gate (see
    ``AccountAdapter.send_verification_code_sms``): no valid ``SMS_CONSENT``
    ``UserConsent`` row ⇒ the verification SMS is refused.

    Deliberately a **dual** exception: it is both an ``AccountError`` (so callers
    and tests can identify/catch it as a normal, domain-level account error) and an
    ``allauth.core.exceptions.ImmediateHttpResponse`` (allauth's documented hook for
    aborting a flow mid-adapter-call with a specific ``HttpResponse``). Raising it from
    inside an ``AccountAdapter`` hook is caught by allauth's
    ``allauth.account.middleware.AccountMiddleware.process_exception`` and turned into
    the well-formed 4xx response below — instead of propagating as an unhandled 500 —
    regardless of which headless view/stage triggered the SMS send (signup phone-verify
    stage, resend, or authenticated change-phone all funnel through the same adapter
    call). The response body carries ``code="consent_required"`` so the frontend can
    distinguish this error and route the user to the consent step.

    The response is built as a plain ``JsonResponse`` (not allauth's headless
    ``APIResponse``) so this error can be raised and inspected outside of an active
    request context too (e.g. directly against the adapter in unit tests) without
    depending on request-scoped state such as session/token metadata.
    """

    code = "consent_required"
    default_message = "SMS consent is required before a verification code can be sent."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        Exception.__init__(self, self.message)
        ImmediateHttpResponse.__init__(self, response=self._build_response())

    def _build_response(self) -> JsonResponse:
        return JsonResponse(
            {
                "status": int(HTTPStatus.FORBIDDEN),
                "errors": [{"code": self.code, "message": self.message}],
            },
            status=HTTPStatus.FORBIDDEN,
        )

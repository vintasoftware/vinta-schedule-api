from http import HTTPStatus

from django.http import JsonResponse

from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.internal.restkit.response import APIResponse


class AccountError(Exception):
    """Base exception for account related errors"""

    pass


class UserNotAuthenticatedError(AccountError):
    def __init__(self, message="User must be authenticated to generate an access token."):
        super().__init__(message)


class ConsentRequiredError(AccountError, ImmediateHttpResponse):
    """Raised by ``AccountAdapter.send_verification_code_sms`` to refuse an SMS
    send when the user has no recorded ``SMS_CONSENT``.

    Dual-inherited: it's an ``AccountError`` (catchable in tests/callers) and an
    ``ImmediateHttpResponse``, so allauth's
    ``account.middleware.AccountMiddleware.process_exception`` turns it into a
    clean 403 ``consent_required`` response instead of an unhandled 500.

    The response body matches allauth headless's own envelope
    (``status``/``errors``/``meta``) when there's a live request to build it
    from. It falls back to a plain envelope with the same ``status``/``errors``
    keys when there isn't, since this error can also be raised and inspected
    outside of a request (e.g. directly against the adapter in unit tests).
    """

    code = "consent_required"
    default_message = "SMS consent is required before a verification code can be sent."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        Exception.__init__(self, self.message)
        ImmediateHttpResponse.__init__(self, response=self._build_response())

    def _build_response(self) -> JsonResponse:
        errors = [{"code": self.code, "message": self.message}]
        request = context.request
        if request is not None:
            try:
                return APIResponse(request, errors=errors, status=HTTPStatus.FORBIDDEN)
            except AttributeError:
                # request isn't a fully-formed headless request (e.g. missing
                # request.allauth.headless) - fall through to the plain envelope.
                pass
        return JsonResponse(
            {"status": int(HTTPStatus.FORBIDDEN), "errors": errors},
            status=HTTPStatus.FORBIDDEN,
        )


class VerificationEmailUndeliverableError(AccountError):
    """Raised by ``AccountAdapter.send_confirmation_mail`` when the address-confirmation
    email could not be handed to a working channel.

    Deliberately **not** an ``ImmediateHttpResponse``, unlike ``ConsentRequiredError``
    above. ``allauth.headless.account.views.SignupView.post`` wraps ``complete_signup``
    in ``except ImmediateHttpResponse: pass``, so an error raised as one from inside the
    confirmation-mail send is swallowed there and answered with an envelope built from
    ``resp = None`` -- which looks to the caller exactly like a delivered email. Raised
    as a plain exception it propagates out of the view, where ``AtomicSignupView``
    catches it, rolls the account back and answers 503.

    ``code`` follows the shape allauth's own errors use, so a client's single allauth
    error mapper handles it with no special case.
    """

    code = "verification_email_failed"
    default_message = (
        "We could not send your verification email, so the account was not created. "
        "Please try again in a few minutes."
    )

    def __init__(self, message: str | None = None) -> None:
        self.message = str(message or self.default_message)
        super().__init__(self.message)

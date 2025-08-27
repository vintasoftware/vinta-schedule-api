"""Decorators for calendar service methods."""

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from calendar_integration.services.type_guards import (
    is_calendar_service_authenticated,
    is_calendar_service_initialized_or_authenticated,
    is_calendar_service_initialized_without_provider,
)


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService


def requires_authentication(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that ensures the calendar service is authenticated before calling the method.

    Raises:
        ValueError: If the service is not authenticated.
    """

    @wraps(func)
    def wrapper(self: "CalendarService", *args: Any, **kwargs: Any) -> Any:
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )
        return func(self, *args, **kwargs)

    return wrapper


def requires_initialization(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that ensures the calendar service is initialized without provider before calling the method.

    Raises:
        ValueError: If the service is not initialized without provider.
    """

    @wraps(func)
    def wrapper(self: "CalendarService", *args: Any, **kwargs: Any) -> Any:
        if not is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup without a provider. "
                "Please call `initialize_without_provider` first."
            )
        return func(self, *args, **kwargs)

    return wrapper


def requires_authentication_or_initialization(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that ensures the calendar service is either authenticated or initialized without provider.

    Raises:
        ValueError: If the service is neither authenticated nor initialized without provider.
    """

    @wraps(func)
    def wrapper(self: "CalendarService", *args: Any, **kwargs: Any) -> Any:
        if not (is_calendar_service_initialized_or_authenticated(self)):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )
        return func(self, *args, **kwargs)

    return wrapper

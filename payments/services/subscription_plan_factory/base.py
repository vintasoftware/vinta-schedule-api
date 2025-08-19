from typing import Protocol

from payments.models import Subscription
from payments.services.dataclasses import CreatedPlan


class BaseSubscriptionPlanFactory(Protocol):
    """
    Base class for creating subscription plans.
    """

    def make_plan_from_subscription(self, subscription: Subscription) -> CreatedPlan:
        """
        Create a subscription plan.
        """
        raise NotImplementedError("This method should be overridden by subclasses.")

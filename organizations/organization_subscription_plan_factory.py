from organizations.models import SubscriptionPlan
from payments.models import Subscription
from payments.services.dataclasses import CreatedPlan, Plan
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


class OrganizationSubscriptionPlanFactory(BaseSubscriptionPlanFactory):
    def create_subscription_plan(self, subscription: Subscription):
        # Custom logic for creating a subscription plan for a calendar organization
        subscription_plan = (
            SubscriptionPlan.objects.filter(subscription=subscription)
            .select_related("organization")
            .first()
        )
        organization = subscription_plan.organization

        subscription_plan_data = {
            "id": organization.pk,
            "name": organization.name,
            "value": subscription_plan.value,
            "currency": "BRL",
            "billing_day": subscription_plan.billing_day,
        }

        if not subscription_plan.plan_external_id:
            return Plan(**subscription_plan_data)

        return CreatedPlan(**subscription_plan_data, external_id=subscription_plan.plan_external_id)

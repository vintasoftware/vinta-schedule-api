"""``SubscriptionAdmin.save_formset`` — verifies the claim its docstring makes
(Phase 4 review SHOULD-FIX 9): a row an admin merely viewed without changing is
not returned by ``formset.save(commit=False)`` and therefore is not stamped
``is_overridden=True``. A wrong answer here stamps every row on save and
effectively freezes the whole subscription against future plan changes.
"""

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.admin import (
    SubscriptionAdmin,
    SubscriptionEntitlementInline,
    SubscriptionPlanLimitInline,
)
from payments.billing_constants import Entitlement, LimitedResource
from payments.models import Subscription
from payments.services.subscription_service import SubscriptionService


@pytest.fixture
def superuser():
    return get_user_model().objects.create_superuser(
        email="subscription-admin@example.com",
        password="adminpassword",  # noqa: S106
    )


@pytest.mark.django_db
class TestSubscriptionAdminSaveFormsetLimits:
    def test_only_the_changed_row_is_marked_overridden(self, rf, superuser):
        org = baker.make(Organization, parent=None)
        subscription = SubscriptionService().create_subscription_for_organization(org)
        changed_row = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        unchanged_row = subscription.limits.exclude(pk=changed_row.pk).first()
        assert unchanged_row is not None

        admin_instance = SubscriptionAdmin(Subscription, AdminSite())
        inline = SubscriptionPlanLimitInline(Subscription, AdminSite())
        request = rf.post(f"/admin/payments/subscription/{subscription.pk}/change/")
        request.user = superuser
        formset_class = inline.get_formset(request, subscription)

        data = {
            "limits-TOTAL_FORMS": "2",
            "limits-INITIAL_FORMS": "2",
            "limits-MIN_NUM_FORMS": "0",
            "limits-MAX_NUM_FORMS": "1000",
            "limits-0-id": str(changed_row.pk),
            "limits-0-subscription": str(subscription.pk),
            "limits-0-resource_key": changed_row.resource_key,
            "limits-0-limit_value": "999",
            "limits-0-kind": changed_row.kind,
            "limits-1-id": str(unchanged_row.pk),
            "limits-1-subscription": str(subscription.pk),
            "limits-1-resource_key": unchanged_row.resource_key,
            "limits-1-limit_value": (
                "" if unchanged_row.limit_value is None else str(unchanged_row.limit_value)
            ),
            "limits-1-kind": unchanged_row.kind,
        }
        formset = formset_class(data, instance=subscription, prefix="limits")
        assert formset.is_valid(), formset.errors

        admin_instance.save_formset(request, form=None, formset=formset, change=True)

        changed_row.refresh_from_db()
        unchanged_row.refresh_from_db()
        assert changed_row.limit_value == 999
        assert changed_row.is_overridden is True
        assert unchanged_row.is_overridden is False

    def test_unchecking_is_overridden_alone_actually_clears_it(self, rf, superuser):
        """Phase 4 verification review BLOCKER: the previous ``save_formset`` was
        documented as letting an admin clear ``is_overridden`` by unchecking the
        box, but always re-stamped it ``True`` because unchecking the box makes
        ``form.has_changed()`` true, landing the row in ``formset.save(commit=False)``.
        This proves the fix: a save where ``is_overridden`` is the *only* changed
        field must leave the new (cleared) value in place.
        """
        org = baker.make(Organization, parent=None)
        subscription = SubscriptionService().create_subscription_for_organization(org)
        overridden_row = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        overridden_row.is_overridden = True
        overridden_row.save(update_fields=["is_overridden"])
        other_row = subscription.limits.exclude(pk=overridden_row.pk).first()
        assert other_row is not None

        admin_instance = SubscriptionAdmin(Subscription, AdminSite())
        inline = SubscriptionPlanLimitInline(Subscription, AdminSite())
        request = rf.post(f"/admin/payments/subscription/{subscription.pk}/change/")
        request.user = superuser
        formset_class = inline.get_formset(request, subscription)

        data = {
            "limits-TOTAL_FORMS": "2",
            "limits-INITIAL_FORMS": "2",
            "limits-MIN_NUM_FORMS": "0",
            "limits-MAX_NUM_FORMS": "1000",
            "limits-0-id": str(overridden_row.pk),
            "limits-0-subscription": str(subscription.pk),
            "limits-0-resource_key": overridden_row.resource_key,
            "limits-0-limit_value": (
                "" if overridden_row.limit_value is None else str(overridden_row.limit_value)
            ),
            "limits-0-kind": overridden_row.kind,
            # is_overridden intentionally omitted: an unchecked checkbox is not
            # submitted, which is how the admin clears it.
            "limits-1-id": str(other_row.pk),
            "limits-1-subscription": str(subscription.pk),
            "limits-1-resource_key": other_row.resource_key,
            "limits-1-limit_value": (
                "" if other_row.limit_value is None else str(other_row.limit_value)
            ),
            "limits-1-kind": other_row.kind,
        }
        formset = formset_class(data, instance=subscription, prefix="limits")
        assert formset.is_valid(), formset.errors

        admin_instance.save_formset(request, form=None, formset=formset, change=True)

        overridden_row.refresh_from_db()
        other_row.refresh_from_db()
        assert overridden_row.is_overridden is False
        assert other_row.is_overridden is False


@pytest.mark.django_db
class TestSubscriptionAdminSaveFormsetEntitlements:
    def test_only_the_changed_row_is_marked_overridden(self, rf, superuser):
        org = baker.make(Organization, parent=None)
        subscription = SubscriptionService().create_subscription_for_organization(org)
        changed_row = subscription.entitlements.get(
            entitlement_key=Entitlement.EXTERNAL_CALENDAR_GOOGLE
        )
        unchanged_row = subscription.entitlements.exclude(pk=changed_row.pk).first()
        assert unchanged_row is not None

        admin_instance = SubscriptionAdmin(Subscription, AdminSite())
        inline = SubscriptionEntitlementInline(Subscription, AdminSite())
        request = rf.post(f"/admin/payments/subscription/{subscription.pk}/change/")
        request.user = superuser
        formset_class = inline.get_formset(request, subscription)

        data = {
            "entitlements-TOTAL_FORMS": "2",
            "entitlements-INITIAL_FORMS": "2",
            "entitlements-MIN_NUM_FORMS": "0",
            "entitlements-MAX_NUM_FORMS": "1000",
            "entitlements-0-id": str(changed_row.pk),
            "entitlements-0-subscription": str(subscription.pk),
            "entitlements-0-entitlement_key": changed_row.entitlement_key,
            # Flip is_enabled from True to False.
            "entitlements-1-id": str(unchanged_row.pk),
            "entitlements-1-subscription": str(subscription.pk),
            "entitlements-1-entitlement_key": unchanged_row.entitlement_key,
            "entitlements-1-is_enabled": "on" if unchanged_row.is_enabled else "",
        }
        formset = formset_class(data, instance=subscription, prefix="entitlements")
        assert formset.is_valid(), formset.errors

        admin_instance.save_formset(request, form=None, formset=formset, change=True)

        changed_row.refresh_from_db()
        unchanged_row.refresh_from_db()
        assert changed_row.is_enabled is False
        assert changed_row.is_overridden is True
        assert unchanged_row.is_overridden is False

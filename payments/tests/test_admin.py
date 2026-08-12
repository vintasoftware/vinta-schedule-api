"""``SubscriptionAdmin.save_formset`` — verifies the claim its docstring makes: a
row an admin merely viewed without changing is not returned by
``formset.save(commit=False)`` and therefore is not stamped ``is_overridden=True``.
A wrong answer here stamps every row on save and effectively freezes the whole
subscription against future plan changes.
"""

from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker

from payments.admin import (
    BillingPlanAdminForm,
    BillingProfileAdmin,
    PlanLimitInline,
    SubscriptionAdmin,
    SubscriptionEntitlementInline,
    SubscriptionPlanLimitInline,
)
from payments.billing_constants import Entitlement, LimitedResource, LimitKind
from payments.constants import PaymentProviders
from payments.exceptions import UnknownPaymentProviderError
from payments.models import BillingPlan, BillingProfile, PlanLimit, Subscription
from payments.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from payments.services.subscription_service import SubscriptionService
from tenancy.models import Organization


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


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
        """The previous ``save_formset`` was documented as letting an admin clear
        ``is_overridden`` by unchecking the box, but always re-stamped it ``True``
        because unchecking the box makes
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


@pytest.mark.django_db
class TestBillingPlanAdminLimitCoverage:
    """``BillingPlanAdmin`` must refuse to author an incomplete plan.

    Plan completeness is the rule that stops a downgrade from handing a resource an
    infinite ceiling, and the admin is the one surface where a support admin can
    introduce a gap. Checked on the *inline formset* rather than on the parent form:
    the parent is validated before the inline rows are saved, so ``BillingPlan.clean``
    there would reject the very edit that completes the plan.
    """

    def _formset_data(self, resource_keys, prefix="limits"):
        data = {
            f"{prefix}-TOTAL_FORMS": str(len(resource_keys)),
            f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for index, resource_key in enumerate(resource_keys):
            data[f"{prefix}-{index}-resource_key"] = resource_key
            data[f"{prefix}-{index}-limit_value"] = "0"
            data[f"{prefix}-{index}-kind"] = LimitKind.PREPAID
        return data

    def _formset_class(self, rf, superuser, plan):
        inline = PlanLimitInline(BillingPlan, AdminSite())
        request = rf.post(f"/admin/payments/billingplan/{plan.pk}/change/")
        request.user = superuser
        return inline.get_formset(request, plan)

    def test_a_formset_omitting_a_resource_is_rejected(self, rf, superuser):
        plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        submitted = [
            key for key in LimitedResource.values if key != LimitedResource.RESOURCE_CALENDARS
        ]
        formset_class = self._formset_class(rf, superuser, plan)

        formset = formset_class(self._formset_data(submitted), instance=plan, prefix="limits")

        assert not formset.is_valid()
        assert LimitedResource.RESOURCE_CALENDARS in str(formset.non_form_errors())

    def test_a_formset_covering_every_resource_is_accepted(self, rf, superuser):
        plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        formset_class = self._formset_class(rf, superuser, plan)

        formset = formset_class(
            self._formset_data(list(LimitedResource.values)), instance=plan, prefix="limits"
        )

        assert formset.is_valid(), formset.errors

    def test_deleting_a_row_that_would_leave_a_gap_is_rejected(self, rf, superuser):
        """The check runs against the rows the save is about to produce, so removing
        coverage is caught as surely as never adding it."""
        plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        rows = [
            baker.make(
                PlanLimit,
                plan=plan,
                resource_key=resource_key,
                limit_value=0,
                kind=LimitKind.PREPAID,
            )
            for resource_key in LimitedResource.values
        ]
        data = {
            "limits-TOTAL_FORMS": str(len(rows)),
            "limits-INITIAL_FORMS": str(len(rows)),
            "limits-MIN_NUM_FORMS": "0",
            "limits-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            data[f"limits-{index}-id"] = str(row.pk)
            data[f"limits-{index}-plan"] = str(plan.pk)
            data[f"limits-{index}-resource_key"] = row.resource_key
            data[f"limits-{index}-limit_value"] = "0"
            data[f"limits-{index}-kind"] = row.kind
        data["limits-0-DELETE"] = "on"
        formset_class = self._formset_class(rf, superuser, plan)

        formset = formset_class(data, instance=plan, prefix="limits")

        assert not formset.is_valid()
        assert rows[0].resource_key in str(formset.non_form_errors())

    def test_the_parent_form_does_not_block_fixing_an_incomplete_plan(self, rf, superuser):
        """``BillingPlan.clean`` would reject an existing incomplete plan on the
        parent form — before the inline rows that complete it are saved — leaving no
        way to fix the plan through the admin at all. The admin form opts that one
        check out; the formset above is what enforces it."""
        plan = baker.make(BillingPlan, is_default_for_new_organizations=False, slug="gappy")
        assert plan.get_missing_limited_resource_keys()

        form = BillingPlanAdminForm(
            data={
                "slug": plan.slug,
                "name": plan.name,
                "is_active": "on",
                "monthly_price": "10.00",
                "currency": "USD",
            },
            instance=plan,
        )

        assert form.is_valid(), form.errors

    def test_get_extra_pre_renders_one_blank_row_per_missing_resource(self, rf, superuser):
        """With `extra = 0`, fixing an incomplete plan required clicking "Add
        another" once per missing row before a single row could be saved. One blank
        row per gap should already be on the page."""
        plan = baker.make(BillingPlan, is_default_for_new_organizations=False, slug="gappy-extra")
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=0,
            kind=LimitKind.PREPAID,
        )
        missing_count = len(plan.get_missing_limited_resource_keys())
        assert missing_count > 0

        inline = PlanLimitInline(BillingPlan, AdminSite())
        request = rf.get(f"/admin/payments/billingplan/{plan.pk}/change/")
        request.user = superuser

        assert inline.get_extra(request, plan) == missing_count

    def test_an_incomplete_plan_can_be_deactivated_without_adding_rows(self, rf, superuser):
        """Retiring a broken plan -- setting `is_active=False` -- must not be blocked
        by the coverage check, or an incomplete plan can never be taken out of service
        through the admin without first backfilling every missing row."""
        plan = baker.make(
            BillingPlan,
            is_default_for_new_organizations=False,
            is_active=True,
            slug="gappy-retire",
        )
        row = baker.make(
            PlanLimit,
            plan=plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=0,
            kind=LimitKind.PREPAID,
        )
        assert plan.get_missing_limited_resource_keys()
        plan.is_active = False
        formset_class = self._formset_class(rf, superuser, plan)

        data = {
            "limits-TOTAL_FORMS": "1",
            "limits-INITIAL_FORMS": "1",
            "limits-MIN_NUM_FORMS": "0",
            "limits-MAX_NUM_FORMS": "1000",
            "limits-0-id": str(row.pk),
            "limits-0-plan": str(plan.pk),
            "limits-0-resource_key": row.resource_key,
            "limits-0-limit_value": "0",
            "limits-0-kind": row.kind,
        }
        formset = formset_class(data, instance=plan, prefix="limits")

        assert formset.is_valid(), formset.errors

    def test_activating_an_incomplete_plan_is_still_rejected(self, rf, superuser):
        """The retirement escape hatch is one-directional: flipping
        `is_active` back to `True` on an incomplete plan must still go through
        the coverage check."""
        plan = baker.make(
            BillingPlan,
            is_default_for_new_organizations=False,
            is_active=False,
            slug="gappy-activate",
        )
        row = baker.make(
            PlanLimit,
            plan=plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=0,
            kind=LimitKind.PREPAID,
        )
        plan.is_active = True
        formset_class = self._formset_class(rf, superuser, plan)

        data = {
            "limits-TOTAL_FORMS": "1",
            "limits-INITIAL_FORMS": "1",
            "limits-MIN_NUM_FORMS": "0",
            "limits-MAX_NUM_FORMS": "1000",
            "limits-0-id": str(row.pk),
            "limits-0-plan": str(plan.pk),
            "limits-0-resource_key": row.resource_key,
            "limits-0-limit_value": "0",
            "limits-0-kind": row.kind,
        }
        formset = formset_class(data, instance=plan, prefix="limits")

        assert not formset.is_valid()


class FakeChangedDataForm:
    """Stand-in for a Django ``ModelForm`` in a ``save_model`` test -- the
    admin's ``save_model`` reads only ``form.changed_data``, so a real
    ``ModelForm`` (with its field cleaning, widgets, etc.) is unnecessary
    ceremony here."""

    def __init__(self, changed_data):
        self.changed_data = changed_data


@pytest.mark.django_db
class TestBillingProfileAdminSaveModel:
    """``BillingProfileAdmin.save_model`` -- verifies each of its documented
    branches: an audited edit to ``payment_provider``, a no-op for edits that
    don't touch it, the create path (no audited route), and clearing the field
    back to "" (a legitimate un-pin, not a 500 -- see the Finding 2 fix in the
    Payment Provider Selection Phase 2 review).
    """

    @pytest.fixture
    def billing_profile(self):
        organization = baker.make(Organization, parent=None)
        billing_address = baker.make("payments.BillingAddress")
        return baker.make(
            BillingProfile,
            organization=organization,
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

    def test_editing_payment_provider_routes_through_set_payment_provider(
        self, rf, superuser, billing_profile
    ):
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post(f"/admin/payments/billingprofile/{billing_profile.pk}/change/")
        request.user = superuser
        billing_profile.payment_provider = PaymentProviders.STRIPE
        form = FakeChangedDataForm(changed_data=["payment_provider"])

        with patch.object(
            SubscriptionService, "set_payment_provider", autospec=True
        ) as mock_set_payment_provider:
            mock_set_payment_provider.return_value = billing_profile
            admin_instance.save_model(request, billing_profile, form, change=True)

        mock_set_payment_provider.assert_called_once()
        call_args = mock_set_payment_provider.call_args
        assert call_args.args[1] == billing_profile.organization
        assert call_args.args[2] == PaymentProviders.STRIPE
        assert call_args.kwargs["actor"] == superuser

    def test_editing_an_unrelated_field_does_not_call_set_payment_provider(
        self, rf, superuser, billing_profile
    ):
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post(f"/admin/payments/billingprofile/{billing_profile.pk}/change/")
        request.user = superuser
        billing_profile.document_number = "99988877766"
        form = FakeChangedDataForm(changed_data=["document_number"])

        with patch.object(
            SubscriptionService, "set_payment_provider", autospec=True
        ) as mock_set_payment_provider:
            admin_instance.save_model(request, billing_profile, form, change=True)

        mock_set_payment_provider.assert_not_called()
        billing_profile.refresh_from_db()
        assert billing_profile.document_number == "99988877766"
        # The pin is untouched -- the plain `super().save_model()` path ran instead.
        assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO

    def test_creating_a_profile_with_a_provider_persists_without_the_audited_path(
        self, rf, superuser
    ):
        organization = baker.make(Organization, parent=None)
        billing_address = baker.make("payments.BillingAddress")
        new_profile = BillingProfile(
            organization=organization,
            contact_first_name="Ada",
            contact_email="ada@example.com",
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider=PaymentProviders.STRIPE,
        )
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post("/admin/payments/billingprofile/add/")
        request.user = superuser
        form = FakeChangedDataForm(changed_data=["organization", "payment_provider"])

        with patch.object(
            SubscriptionService, "set_payment_provider", autospec=True
        ) as mock_set_payment_provider:
            admin_instance.save_model(request, new_profile, form, change=False)

        mock_set_payment_provider.assert_not_called()
        persisted = BillingProfile.objects.get(pk=organization.pk)
        assert persisted.payment_provider == PaymentProviders.STRIPE

    def test_clearing_payment_provider_unpins_instead_of_500ing(
        self, rf, superuser, billing_profile, django_capture_on_commit_callbacks
    ):
        """A cleared ``<select>`` submits ``payment_provider=""``. Before the
        Finding 2 fix this reached ``set_payment_provider("")``, which raised
        ``UnknownPaymentProviderError`` (uncaught -> HTTP 500) because "" is not
        a member of ``PaymentProviders``. It must instead un-pin the profile."""
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post(f"/admin/payments/billingprofile/{billing_profile.pk}/change/")
        request.user = superuser
        billing_profile.payment_provider = ""
        form = FakeChangedDataForm(changed_data=["payment_provider"])

        with patch("audit.services.persist_audit_record"):
            with django_capture_on_commit_callbacks(execute=True):
                admin_instance.save_model(request, billing_profile, form, change=True)

        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == ""

    def test_repointing_to_a_provider_with_no_outbound_credential_does_not_500(
        self, rf, superuser, billing_profile, django_capture_on_commit_callbacks, di_container
    ):
        """Payment Provider Selection, Phase 4: ``set_payment_provider`` validates
        **registry membership only**. A staff repoint onto a provider whose secret
        key this environment has not been given yet is legitimate -- the pin only
        governs *future* charges, and "flip the pin, then add the key" is a normal
        ordering. Asserting the credential here would raise
        ``PaymentProviderNotConfiguredError``, which
        ``BillingProfileAdmin.save_model`` does not catch (HTTP 500 in admin).

        Drives a real ``StripePaymentAdapter`` built with an empty ``api_key`` --
        exactly what the DI container produces when ``STRIPE_SECRET_KEY`` is unset.
        """
        unconfigured_stripe = StripePaymentAdapter(api_key="", webhook_secret="")
        assert unconfigured_stripe.is_configured is False
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post(f"/admin/payments/billingprofile/{billing_profile.pk}/change/")
        request.user = superuser
        billing_profile.payment_provider = PaymentProviders.STRIPE
        form = FakeChangedDataForm(changed_data=["payment_provider"])

        with di_container.stripe_payment_gateway.override(unconfigured_stripe):
            with patch("audit.services.persist_audit_record"):
                with django_capture_on_commit_callbacks(execute=True):
                    admin_instance.save_model(request, billing_profile, form, change=True)

        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == PaymentProviders.STRIPE

    def test_repointing_to_a_slug_that_is_not_a_provider_still_raises(
        self, rf, superuser, billing_profile
    ):
        """The other side of the same line: registry membership *is* enforced. A
        slug no adapter is registered for is bad data, not a deploy-ordering
        question, and must not be silently pinned."""
        admin_instance = BillingProfileAdmin(BillingProfile, AdminSite())
        request = rf.post(f"/admin/payments/billingprofile/{billing_profile.pk}/change/")
        request.user = superuser
        billing_profile.payment_provider = "not_a_real_provider"
        form = FakeChangedDataForm(changed_data=["payment_provider"])

        with pytest.raises(UnknownPaymentProviderError):
            admin_instance.save_model(request, billing_profile, form, change=True)

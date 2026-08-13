"""The Django user admin does not offer the organization groups.

Carried forward from Phase 4 of the vinta-django-orgs migration and decided
here: the three seeded ``auth.Group`` rows are meaningful only when they hang
off an ``OrganizationMembership``. On a *user* they grant nothing, and offering
them in the user form's group picker is a control whose only possible outcome
is a wrong belief about who administers what.
"""

from __future__ import annotations

from django.contrib.admin.sites import site
from django.contrib.auth.models import Group
from django.test import Client, RequestFactory
from django.urls import reverse

import pytest
from model_bakery import baker

from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
    GROUP_PERMISSIONS,
)
from users.factories import UserFactory
from users.models import User


@pytest.mark.django_db
class TestTheUserFormsGroupPicker:
    def _groups_field(self):
        """The ``groups`` field of the real admin *change* form.

        ``obj`` must be passed: without one, ``UserAdmin.get_form`` returns the
        *add* form, whose fieldsets are email and password only. The request
        must carry a staff user, because ``get_form`` consults the add/change
        permissions on it.
        """
        request = RequestFactory().get("/admin/users/user/1/change/")
        request.user = baker.make(User, is_staff=True, is_superuser=True)
        user_admin = site._registry[User]
        form = user_admin.get_form(request=request, obj=baker.make(User))
        return form().fields["groups"]

    def test_the_seeded_organization_groups_are_not_offered(self):
        offered = {group.name for group in self._groups_field().queryset}

        assert GROUP_ORGANIZATION_ADMIN not in offered
        assert GROUP_ORGANIZATION_BILLING_OWNER not in offered
        assert GROUP_ORGANIZATION_MEMBER not in offered

    def test_any_other_group_is_still_offered(self):
        """The narrowing is the three seeded names, not the whole widget.

        Without this the test above would pass just as well if somebody
        emptied the picker entirely.
        """
        Group.objects.create(name="support_staff")

        offered = {group.name for group in self._groups_field().queryset}

        assert "support_staff" in offered


@pytest.mark.django_db
class TestSavingTheChangeFormDoesNotStripAnExistingSeededGroup:
    """Round-trip, not a queryset assertion.

    ``ModelMultipleChoiceField`` only renders (and, via ``save_m2m``'s
    ``.set()``, only *keeps*) options present in the field's ``queryset``.
    Excluding the seeded groups outright -- what ``formfield_for_manytomany``
    used to do unconditionally -- silently strips an existing seeded-group
    assignment the moment a real change form is posted for any other reason.
    Proven end-to-end here through the actual admin view, not by inspecting
    the widget's queryset.
    """

    @pytest.fixture
    def admin_client(self):
        superuser = UserFactory().create_user(email="staff@example.com")
        superuser.is_staff = True
        superuser.is_superuser = True
        superuser.save(update_fields=["is_staff", "is_superuser"])
        client = Client()
        client.force_login(superuser)
        return client

    def test_the_add_form_falls_back_to_the_plain_exclusion_without_an_object(self):
        """``UserAdmin``'s ``add_fieldsets`` never renders ``groups`` at all, so
        there is no add-form URL to round-trip against. This exercises the
        ``obj is None`` branch directly, the way the *add* view would reach it
        if a deployment's ``add_fieldsets`` ever grew a ``groups`` field.
        """
        request = RequestFactory().get("/super/users/user/add/")
        request.user = baker.make(User, is_staff=True, is_superuser=True)
        user_admin = site._registry[User]
        db_field = User._meta.get_field("groups")

        formfield = user_admin.formfield_for_manytomany(db_field, request)

        offered = {group.pk for group in formfield.queryset}
        seeded = set(Group.objects.filter(name__in=GROUP_PERMISSIONS).values_list("pk", flat=True))
        assert not (offered & seeded)

    def test_change_form_offers_a_group_the_target_already_holds(self, admin_client):
        seeded_group = Group.objects.get(name=GROUP_ORGANIZATION_ADMIN)
        target = UserFactory().create_user(email="member@example.com")
        target.groups.add(seeded_group)
        change_url = reverse("admin:users_user_change", args=[target.pk])

        response = admin_client.get(change_url)

        offered = {
            group.pk for group in response.context["adminform"].form.fields["groups"].queryset
        }
        assert seeded_group.pk in offered

    def test_an_ordinary_unrelated_edit_leaves_the_seeded_group_assigned(self, admin_client):
        seeded_group = Group.objects.get(name=GROUP_ORGANIZATION_ADMIN)
        target = UserFactory().create_user(email="member@example.com")
        target.is_active = True
        target.save(update_fields=["is_active"])
        target.groups.add(seeded_group)
        change_url = reverse("admin:users_user_change", args=[target.pk])

        response = admin_client.post(
            change_url,
            data={
                "email": "member-renamed@example.com",
                "is_active": "on",
                "groups": [str(seeded_group.pk)],
            },
        )

        assert response.status_code == 302, getattr(
            response.context.get("adminform").form if response.context else None, "errors", None
        )
        target.refresh_from_db()
        assert target.email == "member-renamed@example.com"
        assert target.groups.filter(pk=seeded_group.pk).exists()

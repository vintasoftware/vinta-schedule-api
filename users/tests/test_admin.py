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
from django.test import RequestFactory

import pytest
from model_bakery import baker

from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
)
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

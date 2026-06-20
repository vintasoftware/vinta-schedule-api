import django_virtual_models as v

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership


class OrganizationVirtualModel(v.VirtualModel):
    class Meta:
        model = Organization


class OrganizationMembershipVirtualModel(v.VirtualModel):
    class Meta:
        model = OrganizationMembership


class OrganizationInvitationVirtualModel(v.VirtualModel):
    class Meta:
        model = OrganizationInvitation

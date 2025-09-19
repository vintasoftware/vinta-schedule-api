import django_virtual_models as v

from organizations.models import Organization, OrganizationInvitation


class OrganizationVirtualModel(v.VirtualModel):
    class Meta:
        model = Organization


class OrganizationInvitationVirtualModel(v.VirtualModel):
    class Meta:
        model = OrganizationInvitation

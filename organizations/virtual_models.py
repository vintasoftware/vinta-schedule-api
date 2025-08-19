import django_virtual_models as v

from organizations.models import Organization


class OrganizationVirtualModel(v.VirtualModel):
    class Meta:
        model = Organization

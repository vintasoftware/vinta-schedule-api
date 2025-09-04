from django.http import HttpRequest

from organizations.models import Organization
from public_api.models import SystemUser


class PublicApiHttpRequest(HttpRequest):
    public_api_system_user: SystemUser | None
    public_api_organization: Organization | None

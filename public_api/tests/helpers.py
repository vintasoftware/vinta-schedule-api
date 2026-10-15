"""Helpers shared by the public API tests."""

import uuid
from collections.abc import Callable, Iterable

from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


GraphQLPost = Callable[[str, dict], dict]


def make_token_post(
    organization: Organization | None,
    resources: Iterable[str],
    *,
    scoped_to_membership: OrganizationMembership | None = None,
    organization_header: int | None = None,
) -> GraphQLPost:
    """Mint a token holding ``resources`` and return a ``post(query, variables)`` for it.

    ``post`` sends the query to ``/graphql/``, checks the response is a 200 and returns
    its JSON body. ``organization=None`` mints the org-less token, which picks its
    organization from the ``X-Public-Api-Organization-Id`` header (``organization_header``).
    """
    from di_core.containers import container

    assert container is not None  # noqa: S101

    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"test_{uuid.uuid4().hex[:8]}",
        organization=organization,
        scoped_to_membership=scoped_to_membership,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    client = APIClient()
    headers = {"authorization": f"Bearer {system_user.id}:{token}"}
    if organization_header is not None:
        headers["X-Public-Api-Organization-Id"] = str(organization_header)

    def post(query: str, variables: dict) -> dict:
        with container.public_api_auth_service.override(auth_service):
            response = client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers=headers,
            )
        assert response.status_code == 200
        return response.json()

    return post

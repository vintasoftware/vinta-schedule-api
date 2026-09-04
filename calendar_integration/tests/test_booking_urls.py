"""Acceptance test for the ``public/booking/`` mount point (Phase 0 scaffolding).

Phase 0 registers no viewsets on ``calendar_integration.booking_urls``'s
``DefaultRouter`` -- see that module's docstring. This is the acceptance test
named in the plan: "GET /public/booking/ resolves (empty router, 404 on any
sub-path)".
"""

import pytest


@pytest.mark.django_db
def test_public_booking_root_resolves(client):
    """``GET /public/booking/`` resolves to DRF's ``APIRootView``, not a 404.

    ``DefaultRouter.APIRootView`` inherits DRF's
    ``DEFAULT_PERMISSION_CLASSES = [IsAuthenticated]`` (this project's
    default), and this mount point is not one of the ``public/booking/``
    viewsets that override it -- there are none yet. So an anonymous GET
    correctly resolves the URL (proving the mount point is wired) and then
    gets refused by the permission check: 401, not 200. That 401 -- not a
    404 -- is what "resolves" means for this empty router.
    """
    response = client.get("/public/booking/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_public_booking_unregistered_subpath_404s(client):
    """Any sub-path 404s -- Phase 0 registers no viewsets on the router."""
    response = client.get("/public/booking/does-not-exist/")
    assert response.status_code == 404

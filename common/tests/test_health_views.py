from django.test import Client, override_settings
from django.urls import reverse


def test_healthz_returns_ok_without_touching_the_database():
    """The load balancer polls this every 30 seconds; a query here would put the
    API's health behind the database's availability rather than its own.

    No `django_db` mark on purpose -- that is what enforces the claim. A view that
    reached the database would fail here with "Database access not allowed"."""
    response = Client().get(reverse("healthz"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz/$"])
def test_healthz_is_not_redirected_to_https():
    """Mirrors the deployed configuration (see settings/production.py). The load
    balancer health-checks over plain HTTP and counts a 301 as unhealthy, so the
    exemption pattern has to match the path SecurityMiddleware actually sees --
    which has had its leading slash stripped, hence `^healthz/$` and not
    `^/healthz/$`."""
    response = Client().get(reverse("healthz"))

    assert response.status_code == 200


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz/$"])
def test_other_paths_are_still_redirected_to_https():
    """The exemption is one path wide, not a hole in the HTTPS policy."""
    response = Client().get("/graphql/")

    assert response.status_code == 301
    assert response["Location"].startswith("https://")

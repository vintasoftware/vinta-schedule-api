"""The *documented* contract is the point of these tests, not merely the
runtime body -- a client generating a typed SDK off ``schema.yml`` needs
``code`` in the schema, not just in what the server happens to return.

Generates the OpenAPI schema in-process (mirroring
``common/tests/test_openapi.py``'s ``SchemaGenerator`` pattern) rather than
reading the checked-in ``schema.yml`` off disk, so this stays correct whether
or not the file on disk has been regenerated in the working tree -- it always
reflects what ``manage.py spectacular`` would produce right now.
"""

import pytest
from drf_spectacular.generators import SchemaGenerator


def _get_schema() -> dict:
    generator = SchemaGenerator()
    return generator.get_schema(request=None, public=True)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    return _get_schema()


def _resolve_response_schema(
    openapi_schema: dict, path: str, method: str, status_code: str
) -> dict:
    """The ``$ref``-resolved schema dict for one operation's response body."""
    operation = openapi_schema["paths"][path][method]
    response_schema = operation["responses"][status_code]["content"]["application/json"]["schema"]
    ref = response_schema["$ref"]
    component_name = ref.rsplit("/", 1)[-1]
    return openapi_schema["components"]["schemas"][component_name]


class TestBillingErrorBodyIsDocumented:
    """``code`` must be a documented property of the 400 bodies --
    ``change-plan``'s ``payment_token_required`` and add-on purchase's
    ``add_on_not_purchasable``."""

    def test_change_plan_400_documents_code(self, openapi_schema: dict) -> None:
        schema = _resolve_response_schema(
            openapi_schema, "/billing/subscription/change-plan/", "post", "400"
        )

        assert "code" in schema["properties"]
        assert "code" in schema.get("required", [])

    def test_add_on_create_400_documents_code(self, openapi_schema: dict) -> None:
        schema = _resolve_response_schema(openapi_schema, "/billing/add-ons/", "post", "400")

        assert "code" in schema["properties"]
        assert "code" in schema.get("required", [])

    def test_change_plan_409_documents_code(self, openapi_schema: dict) -> None:
        """``UnconfirmedPlanChangeError`` is what a 409 means here -- a request
        that conflicts with state the caller can resolve. The deployment faults
        moved to 503 in ``vinta-django-billing`` 0.6.0; see below."""
        schema = _resolve_response_schema(
            openapi_schema, "/billing/subscription/change-plan/", "post", "409"
        )

        assert "code" in schema["properties"]

    def test_change_plan_503_documents_code(self, openapi_schema: dict) -> None:
        """``PaymentProviderNotConfiguredError`` / ``IncompleteBillingPlanError``
        share the 503 body: an operator has to fix the deployment, and retrying
        the same request changes nothing until they do."""
        schema = _resolve_response_schema(
            openapi_schema, "/billing/subscription/change-plan/", "post", "503"
        )

        assert "code" in schema["properties"]

    def test_add_on_create_503_documents_code(self, openapi_schema: dict) -> None:
        """The add-on purchase documents no 409 at all -- its only provider fault
        is the unconfigured-provider one, which is a 503."""
        assert "409" not in openapi_schema["paths"]["/billing/add-ons/"]["post"]["responses"]

        schema = _resolve_response_schema(openapi_schema, "/billing/add-ons/", "post", "503")

        assert "code" in schema["properties"]

    def test_change_plan_request_documents_payment_token(self, openapi_schema: dict) -> None:
        """A contract gap this closes: ``payment_token`` finally appears on the
        documented request body, not only the response."""
        operation = openapi_schema["paths"]["/billing/subscription/change-plan/"]["post"]
        request_schema_ref = operation["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        component_name = request_schema_ref.rsplit("/", 1)[-1]
        request_schema = openapi_schema["components"]["schemas"][component_name]

        assert "payment_token" in request_schema["properties"]


class TestDocumentTypeIsDocumentedAsAnEnum:
    """The generated client must see ``document_type`` as an enum, not a free
    string, and the enum must carry every member the API accepts."""

    def test_billing_profile_document_type_is_a_nine_member_enum(
        self, openapi_schema: dict
    ) -> None:
        billing_profile_schema = openapi_schema["components"]["schemas"]["BillingProfile"]
        document_type_ref = billing_profile_schema["properties"]["document_type"]["$ref"]
        component_name = document_type_ref.rsplit("/", 1)[-1]
        enum_schema = openapi_schema["components"]["schemas"][component_name]

        assert set(enum_schema["enum"]) == {
            "CPF",
            "CNPJ",
            "DNI",
            "CI",
            "RUT",
            "SSN",
            "EIN",
            "PASSPORT",
            "OTHER",
        }

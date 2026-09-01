"""The deploy gate's schema check must stay clean.

``render_build.sh`` runs ``manage.py check --deploy --fail-level WARNING``, and
``drf_spectacular.checks.schema_check`` turns every warning raised while
generating the schema into a ``drf_spectacular.W001``. One such warning fails
the build outright, and it fails it on Render -- after the branch is merged,
with no local signal beforehand. This runs the same check in CI so that signal
arrives at test time instead.

The gate is asserted whole rather than only for the routes that first tripped
it: any view in this project that later leaves drf-spectacular guessing fails
the deploy the same way, and it should fail here first.

What first tripped it was ``vinta_billing``'s two ``re_path``-bound provider
webhooks, whose path segments carried no type for drf-spectacular to infer.
This project annotated them from its own ``AppConfig.ready()`` until
vinta-django-billing 0.7.0 declared them upstream; the class below is what says
the pin still carries that fix, so a downgrade fails here rather than on Render.
"""

from collections.abc import Iterator

from django.core.checks import Warning as CheckWarning

import pytest
from drf_spectacular.checks import schema_check
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator


@pytest.fixture
def schema_check_messages() -> Iterator[list[CheckWarning]]:
    """``schema_check``'s output, run against an empty warning cache.

    ``GENERATOR_STATS``'s caches are class attributes that nothing resets
    between generations, so any earlier test that built a schema (there are
    several) would otherwise leak its warnings into this one's result -- and
    this one's into whatever runs next. Reset on both sides.
    """
    GENERATOR_STATS.reset()
    try:
        yield schema_check(app_configs=None)
    finally:
        GENERATOR_STATS.reset()


def test_deploy_schema_check_reports_nothing(schema_check_messages) -> None:
    assert schema_check_messages == [], "\n".join(str(m) for m in schema_check_messages)


class TestPaymentWebhookPathParameters:
    """The two webhook operations vinta-django-billing 0.7.0 typed upstream.

    Asserted separately from the gate above, and against the generated schema
    rather than the package's annotations, so a regression names the parameter
    that lost its type and states what a client actually reads.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/billing/payments/{id}/payment-update/{provider}/",
            "/billing/payments/{id}/subscription-payment-update/{provider}/",
        ],
    )
    def test_webhook_declares_typed_path_parameters(self, path: str) -> None:
        schema = SchemaGenerator().get_schema(request=None, public=True)

        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["post"]["parameters"]
            if parameter["in"] == "path"
        }

        assert set(parameters) == {"id", "provider"}
        # `integer`, not the `string` drf-spectacular defaulted to while warning:
        # it is a `vinta_billing.Payment` id, and that model inherits `BaseModel`
        # under the package's `BigAutoField` default.
        assert parameters["id"]["schema"] == {"type": "integer"}
        assert parameters["provider"]["schema"] == {"type": "string"}
        assert all(parameter["required"] for parameter in parameters.values())

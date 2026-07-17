from django.apps import AppConfig
from django.urls import register_converter


class PublicApiConfig(AppConfig):
    name = "public_api"
    verbose_name = "Public API"

    def ready(self) -> None:
        """Register URL converters when Django starts up."""
        from public_api.converters import ConceptDocSlugConverter

        register_converter(ConceptDocSlugConverter, "docs_slug")

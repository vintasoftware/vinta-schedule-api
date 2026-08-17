from django.apps import AppConfig
from django.urls import register_converter

from public_api.converters import ConceptDocSlugConverter


class PublicApiConfig(AppConfig):
    name = "public_api"
    verbose_name = "Public API"

    def ready(self) -> None:
        """Register URL converters when Django starts up."""
        register_converter(ConceptDocSlugConverter, "docs_slug")

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Export the django-allauth Headless OpenAPI specification to a file. "
        "The spec reflects the current allauth configuration (login methods, enabled "
        "flows, clients) and is meant to drive the frontend API client / codegen."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            default="schema-auth.yml",
            help="Output path (.json, .yaml or .yml). Defaults to ./schema-auth.yml",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Exit with a non-zero status if the file on disk is out of date "
                "instead of writing it. Use as a CI / pre-commit drift guard."
            ),
        )

    def handle(self, *args, **options):
        # Imported lazily so the command only pulls allauth in when actually run.
        from allauth.headless.spec.internal.schema import get_schema

        spec = get_schema()
        self._patch_refresh_token_meta(spec)
        output = Path(options["output"])
        content = self._render(spec, output.suffix)

        if options["check"]:
            current = output.read_text() if output.exists() else None
            if current != content:
                raise CommandError(
                    f"{output} is out of date. Regenerate it with:\n"
                    f"    python manage.py export_allauth_schema -o {output}"
                )
            self.stdout.write(self.style.SUCCESS(f"{output} is up to date."))
            return

        output.write_text(content)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(spec['paths'])} paths to {output} "
                f"({spec['info']['title']} {spec['info'].get('version', '')})".strip()
            )
        )

    @staticmethod
    def _render(spec: dict, suffix: str) -> str:
        """Serialize the spec to YAML or JSON, deterministically (sorted keys)."""
        if suffix in {".yaml", ".yml"}:
            import yaml

            return yaml.dump(spec, Dumper=yaml.Dumper, sort_keys=True)
        return json.dumps(spec, indent=2, sort_keys=True)

    @staticmethod
    def _patch_refresh_token_meta(spec: dict) -> None:
        """Document ``meta.refresh_token`` on successful auth responses.

        allauth ships a static OpenAPI spec whose ``BaseAuthenticationMeta`` only
        declares ``access_token``/``session_token`` — it cannot know that our
        ``AccessAndRefreshTokenStrategy`` (like allauth's own JWT strategy) also adds a
        ``refresh_token`` to the login/auth ``meta`` at runtime. Inject it so the
        generated schema and any frontend codegen match reality.
        """
        meta = spec.get("components", {}).get("schemas", {}).get("BaseAuthenticationMeta")
        if not meta:
            return
        properties = meta.setdefault("properties", {})
        properties.setdefault(
            "refresh_token",
            {
                "description": "The refresh token (`app` clients only).\n",
                "example": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.QV30",
                "type": "string",
            },
        )

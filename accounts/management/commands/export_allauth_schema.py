import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

import yaml
from allauth.headless.spec.internal.schema import get_schema


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
        spec = get_schema()
        self._patch_refresh_token_meta(spec)
        self._patch_post_auth_destination(spec)
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

    @staticmethod
    def _patch_post_auth_destination(spec: dict) -> None:
        """Document the top-level ``destination`` on completed-authentication responses.

        Same reason as ``_patch_refresh_token_meta``: allauth's static spec cannot
        know about a field our own code adds at runtime. Here that field comes from
        ``accounts.middlewares.PostAuthDestinationMiddleware``, which appends the
        organization's server-resolved post-authentication destination to every
        response reporting a completed authentication — login, signup, email and
        phone verification alike. The SPA reads it instead of deciding where to
        navigate itself, so it has to appear in the schema the frontend generates
        its client from.

        Every 200 authentication response in the spec resolves to the single
        ``AuthenticatedResponse`` schema (the ``Authenticated``,
        ``AuthenticatedByCode``, ``AuthenticatedByPassword`` and
        ``AuthenticatedByPasswordAnd2FA`` response objects all ``$ref`` it), so one
        property covers the whole surface — and it is marked **required** there,
        because the middleware writes it on every such response and the resolution
        always answers (an organization with no configured ``redirect_url``, or a
        user with no organization at all, gets our dashboard). Responses that are
        *not* a completed authentication never carry it, and they are typed by
        different schemas: the 401 ``AuthenticationResponse`` (a pending
        verification stage, a failed login) has no ``destination`` property at all,
        which is exactly the distinction a client should branch on.
        """
        response_schema = spec.get("components", {}).get("schemas", {}).get("AuthenticatedResponse")
        if not response_schema:
            return
        properties = response_schema.setdefault("properties", {})
        properties.setdefault(
            "destination",
            {
                "description": (
                    "Absolute URL the client should navigate to now that the user is "
                    "authenticated: the organization's configured `redirect_url`, or "
                    "our dashboard when it has none. Resolved server-side from the "
                    "organization's branding — never from a client-supplied "
                    "`next`/`callback_url`. Always present on a completed "
                    "authentication (this schema), and never present on the "
                    "interim/failed `AuthenticationResponse`.\n"
                ),
                "example": "https://scheduling.acme.example.com/app",
                "type": "string",
            },
        )
        required = response_schema.setdefault("required", [])
        if "destination" not in required:
            required.append("destination")

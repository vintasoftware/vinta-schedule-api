import json
from pathlib import Path

from django.core.management.base import BaseCommand


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
            default="allauth-openapi.json",
            help="Output path (.json or .yaml). Defaults to ./allauth-openapi.json",
        )

    def handle(self, *args, **options):
        # Imported lazily so the command only pulls allauth in when actually run.
        from allauth.headless.spec.internal.schema import get_schema

        spec = get_schema()
        output = Path(options["output"])

        if output.suffix in {".yaml", ".yml"}:
            import yaml

            content = yaml.dump(spec, Dumper=yaml.Dumper, sort_keys=True)
        else:
            content = json.dumps(spec, indent=2, sort_keys=True)

        output.write_text(content)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(spec['paths'])} paths to {output} "
                f"({spec['info']['title']} {spec['info'].get('version', '')})".strip()
            )
        )

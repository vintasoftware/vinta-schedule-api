from __future__ import annotations

import ast
import importlib
import pathlib

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_GUARD_IMPLEMENTATION_MODULES = {
    "calendar_integration/services/calendar_event_service.py",
    "calendar_integration/services/calendar_service.py",
    "calendar_integration/services/calendar_bundle_service.py",
}

_GUARD_REACHING_CALLS = {
    "create_event",
    "create_recurring_event",
    "check_postpaid_allowance",
}

_ADAPTER_RECEIVER_MARKERS = ("adapter", "client")


# Modules that reach the guarded write path and the test classes that provide
# their behavioral coverage.
#
# The registry is deliberately represented as strings so this command does not
# need to import the test module merely to discover the expected classes.
GUARDED_SURFACES: dict[str, tuple[str, ...]] = {
    "calendar_integration/serializers.py": (
        "TestRestSurface",
        "TestTokenSurface",
    ),
    "calendar_integration/mutations.py": (
        "TestBookingCodeEventSurface",
        "TestBookingCodeGroupEventSurface",
    ),
    "calendar_integration/booking_views.py": (
        "TestBookingCodeRestEventSurface",
        "TestBookingCodeRestGroupEventSurface",
    ),
    "public_api/mutations.py": ("TestPublicApiScheduleEventSurface",),
    "calendar_integration/services/calendar_group_service.py": (
        "TestBookingCodeGroupEventSurface",
    ),
    "calendar_integration/services/calendar_sync_service.py": ("TestBulkSyncWriterSurface",),
}


_TEST_MODULE = "calendar_integration.tests.test_event_creation_surfaces"


def _receiver_name(node: ast.Attribute) -> str:
    """Return the receiver name of an attribute call.

    Examples:
        x.create_event()           -> "x"
        service.create_event()     -> "service"
        foo.service.create_event() -> "service"
    """
    value = node.value

    if isinstance(value, ast.Attribute):
        return value.attr

    if isinstance(value, ast.Name):
        return value.id

    return ""


def _is_excluded_path(path: pathlib.Path) -> bool:
    relative = path.relative_to(_REPO_ROOT)
    parts = relative.parts

    return (
        any(part.startswith(".") for part in parts)
        or "tests" in parts
        or "migrations" in parts
        or "calendar_adapters" in parts
        or path.name == "factories.py"
        or relative.as_posix() in _GUARD_IMPLEMENTATION_MODULES
    )


def _module_reaches_guard(path: pathlib.Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if not isinstance(node.func, ast.Attribute):
            continue

        if node.func.attr not in _GUARD_REACHING_CALLS:
            continue

        receiver = _receiver_name(node.func)

        if any(marker in receiver for marker in _ADAPTER_RECEIVER_MARKERS):
            continue

        return True

    return False


def _first_party_app_dirs() -> list[pathlib.Path]:
    app_dirs = []

    for config in apps.get_app_configs():
        app_path = pathlib.Path(config.path).resolve()

        try:
            app_path.relative_to(_REPO_ROOT)
        except ValueError:
            continue

        app_dirs.append(app_path)

    return app_dirs


def _modules_reaching_guard() -> set[str]:
    found = set()

    for app_dir in _first_party_app_dirs():
        for path in app_dir.rglob("*.py"):
            if _is_excluded_path(path):
                continue

            if _module_reaches_guard(path):
                found.add(path.relative_to(_REPO_ROOT).as_posix())

    return found


def _test_classes() -> dict[str, type]:
    """Return test classes referenced by GUARDED_SURFACES."""
    module = importlib.import_module(_TEST_MODULE)

    classes = {}

    for class_names in GUARDED_SURFACES.values():
        for class_name in class_names:
            if class_name in classes:
                continue

            try:
                classes[class_name] = getattr(module, class_name)
            except AttributeError as exc:
                raise CommandError(
                    f"GUARDED_SURFACES references test class "
                    f"{class_name!r}, but {_TEST_MODULE!r} does not "
                    "define it."
                ) from exc

    return classes


def _check_probe_methods() -> list[str]:
    """Validate that every registered surface has blocked/unlimited probes."""
    errors = []
    classes = _test_classes()

    for module, class_names in GUARDED_SURFACES.items():
        for class_name in class_names:
            test_class = classes[class_name]

            method_names = {name for name in dir(test_class) if name.startswith("test_")}

            if not any("blocked" in name for name in method_names):
                errors.append(
                    f"{module} -> {class_name} has no blocked-path test. "
                    f"Add a test_* method containing 'blocked'."
                )

            if not any("unlimited" in name for name in method_names):
                errors.append(
                    f"{module} -> {class_name} has no unlimited-plan test. "
                    f"Add a test_* method containing 'unlimited'."
                )

    return errors


def _format_modules(modules: set[str]) -> str:
    return "\n".join(f"  - {module}" for module in sorted(modules))


class Command(BaseCommand):
    help = (
        "Statically verify that every first-party module reaching the "
        "post-paid calendar guard has a registered behavioral probe."
    )

    def handle(self, *args, **options):
        discovered = _modules_reaching_guard()
        registered = set(GUARDED_SURFACES)

        unprobed = discovered - registered
        stale = registered - discovered

        errors: list[str] = []

        if unprobed:
            errors.append(
                "Unregistered guarded surfaces:\n"
                f"{_format_modules(unprobed)}\n\n"
                "For each module above, add an entry to "
                "GUARDED_SURFACES with the test class(es) that exercise "
                "that surface."
            )

        if stale:
            errors.append(
                "Stale guarded-surface registrations:\n"
                f"{_format_modules(stale)}\n\n"
                "For each module above, either remove its "
                "GUARDED_SURFACES entry or verify that the guarded call "
                "path has not accidentally been removed."
            )

        errors.extend(_check_probe_methods())

        if errors:
            self.stdout.write(self.style.ERROR("\n\n".join(errors)))
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("Guarded surface static analysis FAILED."))
            raise CommandError("Guarded surface registry is out of date.")

        self.stdout.write(self.style.SUCCESS("Guarded surface static analysis passed."))
        self.stdout.write(f"  Discovered guarded modules: {len(discovered)}")
        self.stdout.write(f"  Registered guarded modules: {len(registered)}")
        self.stdout.write(
            f"  Registered behavioral probes: "
            f"{sum(len(classes) for classes in GUARDED_SURFACES.values())}"
        )

import re
from typing import Literal

from django.db import migrations


class RawSQLSignatureError(Exception):
    """A function or procedure version file does not define the routine it is named for."""


class BaseRawSQLMigrationManager:
    migration_type: Literal["function", "view", "materialized_view", "trigger", "procedure"]
    version: str
    name: str
    drop_command_template: str

    def __init__(self, app_path: str, version: str):
        self.app_path = app_path
        self.version = version

    def dir_name(self) -> str:
        return {
            "function": "functions",
            "view": "views",
            "materialized_view": "materialized_views",
            "trigger": "triggers",
            "procedure": "procedures",
        }[self.migration_type]

    def read_version(self, version: str) -> str:
        """The SQL of one version file of this structure."""
        with open(
            f"./{self.app_path}/migrations/sql/{self.dir_name()}/{self.name}/{version}.sql"
        ) as migration_file:
            return migration_file.read()

    def previous_version(self) -> str | None:
        """The version before this one, or ``None`` for ``0001``."""
        previous_version_int = int(self.version) - 1
        if previous_version_int <= 0:
            return None
        return f"{previous_version_int:04d}"

    def get_forward_sql(self) -> str:
        return self.read_version(self.version)

    def get_backward_sql(self) -> str:
        previous = self.previous_version()
        if previous is None:
            return self.drop_command_template.format(name=self.name)
        return self.read_version(previous)

    def migration(self):
        forward_sql = self.get_forward_sql()
        backward_sql = self.get_backward_sql()
        return migrations.RunSQL(sql=forward_sql, reverse_sql=backward_sql)


# Text that can hold anything, including "CREATE FUNCTION" or a parenthesis, without it
# being SQL: dollar-quoted bodies ($$...$$, $tag$...$tag$), string literals, quoted
# identifiers, and comments.
_OPAQUE_SQL = re.compile(
    r"(?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$).*?(?P=dollar)"
    r"|'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|--[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)


def _mask_opaque_sql(sql: str) -> str:
    """``sql`` with the inside of bodies, literals and comments blanked out.

    Offsets are kept, so a position found in the masked text points at the same place
    in ``sql``.
    """
    return _OPAQUE_SQL.sub(lambda match: " " * len(match.group(0)), sql)


def _split_top_level(text: str, masked: str, separator: str) -> list[str]:
    """Split ``text`` on ``separator`` where ``masked`` has it outside any parentheses
    or brackets."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(masked):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


_DEFAULT_CLAUSE = re.compile(r"\s+DEFAULT\s|\s*=", re.IGNORECASE)


def routine_signatures(sql: str, keyword: str, name: str) -> list[str]:
    """The argument list of each ``CREATE [OR REPLACE] <keyword> <name>(...)`` in ``sql``.

    Each argument keeps its mode, name and type, and loses its default, so the result
    is what ``DROP <keyword> <name>(...)`` takes. Whitespace is collapsed. A signature
    defined twice is listed once. Routines with another name are ignored, and so is
    anything inside a body, a string or a comment.
    """
    masked = _mask_opaque_sql(sql)
    header = re.compile(
        rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?{keyword}\s+(?:\w+\.)?{re.escape(name)}\s*\(",
        re.IGNORECASE,
    )
    signatures: list[str] = []
    for match in header.finditer(masked):
        start = match.end()
        depth = 1
        end = start
        while depth:
            if end >= len(masked):
                raise RawSQLSignatureError(f"Unclosed argument list for {keyword} {name}.")
            if masked[end] == "(":
                depth += 1
            elif masked[end] == ")":
                depth -= 1
            end += 1
        arguments_text, arguments_masked = sql[start : end - 1], masked[start : end - 1]
        arguments = []
        for argument, argument_masked in zip(
            _split_top_level(arguments_text, arguments_masked, ","),
            _split_top_level(arguments_masked, arguments_masked, ","),
            strict=True,
        ):
            default = _DEFAULT_CLAUSE.search(argument_masked)
            if default is not None:
                argument = argument[: default.start()]
            if argument.strip():
                arguments.append(" ".join(argument.split()))
        signature = ", ".join(arguments)
        if signature not in signatures:
            signatures.append(signature)
    return signatures


class RoutineMigrationManager(BaseRawSQLMigrationManager):
    """A function or procedure, which Postgres identifies by name AND argument list.

    ``CREATE OR REPLACE`` only replaces a routine with the same argument list. A
    version that changes the list creates a second routine next to the old one, and
    with defaults on the new arguments the two then make every call ambiguous. So each
    direction of a version change drops the signatures the version it moves away from
    defines and the version it moves to does not. A version that keeps the argument
    list runs exactly its file, as before, so a routine other objects depend on (a
    generated column, an index) is never dropped by a body change.
    """

    routine_keyword: Literal["FUNCTION", "PROCEDURE"]

    def signatures(self, version: str) -> list[str]:
        signatures = routine_signatures(self.read_version(version), self.routine_keyword, self.name)
        if not signatures:
            raise RawSQLSignatureError(
                f"{self.dir_name()}/{self.name}/{version}.sql does not create "
                f"{self.routine_keyword} {self.name}."
            )
        return signatures

    def _drop(self, signatures: list[str]) -> str:
        return "".join(
            f"DROP {self.routine_keyword} IF EXISTS {self.name}({signature});\n"
            for signature in signatures
        )

    def _drop_left_behind(self, leaving: str, arriving: str) -> str:
        """DROP statements for the signatures ``leaving`` has and ``arriving`` lacks."""
        kept = {signature.casefold() for signature in self.signatures(arriving)}
        return self._drop(
            [
                signature
                for signature in self.signatures(leaving)
                if signature.casefold() not in kept
            ]
        )

    def get_forward_sql(self) -> str:
        previous = self.previous_version()
        drops = "" if previous is None else self._drop_left_behind(previous, self.version)
        return drops + self.read_version(self.version)

    def get_backward_sql(self) -> str:
        previous = self.previous_version()
        if previous is None:
            return self._drop(self.signatures(self.version))
        return self._drop_left_behind(self.version, previous) + self.read_version(previous)

    def retirement(self):
        """A migration operation that drops this version's routine for good.

        Use it when a routine is replaced by one under another name. Going back
        creates this version again.
        """
        return migrations.RunSQL(
            sql=self._drop(self.signatures(self.version)),
            reverse_sql=self.read_version(self.version),
        )


class FunctionMigrationManager(RoutineMigrationManager):
    migration_type = "function"
    routine_keyword = "FUNCTION"
    drop_command_template = "DROP FUNCTION IF EXISTS {name};"


class ViewMigrationManager(BaseRawSQLMigrationManager):
    migration_type = "view"
    drop_command_template = "DROP VIEW IF EXISTS {name};"


class MaterializedViewMigrationManager(BaseRawSQLMigrationManager):
    migration_type = "materialized_view"
    drop_command_template = "DROP MATERIALIZED VIEW IF EXISTS {name};"


class TriggerMigrationManager(BaseRawSQLMigrationManager):
    migration_type = "trigger"
    drop_command_template = "DROP TRIGGER IF EXISTS {name};"


class ProcedureMigrationManager(RoutineMigrationManager):
    migration_type = "procedure"
    routine_keyword = "PROCEDURE"
    drop_command_template = "DROP PROCEDURE IF EXISTS {name};"

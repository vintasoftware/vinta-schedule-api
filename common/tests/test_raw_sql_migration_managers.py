"""``FunctionMigrationManager`` / ``ProcedureMigrationManager`` across a signature change.

Postgres identifies a routine by name and argument list. A version that changes the
list must not leave the previous signature behind on the way forward, nor its own on
the way back, or a call matching both becomes ambiguous. A version that keeps the list
must run exactly its file, as it always has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.raw_sql_migration_managers import (
    FunctionMigrationManager,
    ProcedureMigrationManager,
    RawSQLSignatureError,
    routine_signatures,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

FOUR_ARGS = """\
-- CREATE OR REPLACE FUNCTION my_fn(p_commented_out TEXT) is not a definition.
CREATE OR REPLACE FUNCTION my_fn(
    p_org BIGINT,
    p_start TIMESTAMPTZ,
    p_end TIMESTAMPTZ,
    p_limit INTEGER DEFAULT 10
)
RETURNS BOOLEAN AS $$
BEGIN
    EXECUTE 'CREATE FUNCTION my_fn(p_in_a_string TEXT) RETURNS INT AS ''x''';
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE;
"""

SIX_ARGS = """\
CREATE OR REPLACE FUNCTION my_fn(
    p_org BIGINT,
    p_start TIMESTAMPTZ,
    p_end TIMESTAMPTZ,
    p_limit INTEGER DEFAULT 10,
    p_ratio NUMERIC(10, 2) DEFAULT 1.5,
    p_ids BIGINT[] DEFAULT '{}'
)
RETURNS BOOLEAN AS $body$ SELECT TRUE; $body$ LANGUAGE sql STABLE;
"""

SIX_ARGS_NEW_BODY = SIX_ARGS.replace("SELECT TRUE", "SELECT FALSE")

FOUR_SIGNATURE = "p_org BIGINT, p_start TIMESTAMPTZ, p_end TIMESTAMPTZ, p_limit INTEGER"
SIX_SIGNATURE = f"{FOUR_SIGNATURE}, p_ratio NUMERIC(10, 2), p_ids BIGINT[]"


@pytest.fixture
def versions(tmp_path, monkeypatch):
    """Write ``my_fn`` version files under a throwaway app and run from its parent."""
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "app" / "migrations" / "sql" / "functions" / "my_fn"
    directory.mkdir(parents=True)

    def write(**files: str) -> None:
        for version, sql in files.items():
            (directory / f"{version.removeprefix('v')}.sql").write_text(sql)

    return write


class MyFnManager(FunctionMigrationManager):
    name = "my_fn"


def test_signatures_skip_comments_strings_and_bodies_and_drop_defaults():
    assert routine_signatures(FOUR_ARGS, "FUNCTION", "my_fn") == [FOUR_SIGNATURE]
    assert routine_signatures(SIX_ARGS, "FUNCTION", "my_fn") == [SIX_SIGNATURE]


def test_signatures_list_each_overload_once_and_ignore_other_routines():
    sql = (
        FOUR_ARGS
        + FOUR_ARGS
        + "CREATE FUNCTION my_fn_helper(p_x TEXT) RETURNS INT AS $$ SELECT 1 $$ LANGUAGE sql;\n"
        + "CREATE FUNCTION public.my_fn(p_only TEXT) RETURNS INT AS $$ SELECT 1 $$ LANGUAGE sql;\n"
    )

    assert routine_signatures(sql, "FUNCTION", "my_fn") == [FOUR_SIGNATURE, "p_only TEXT"]


def test_a_version_that_changes_the_signature_drops_the_left_behind_one_both_ways(versions):
    versions(v0001=FOUR_ARGS, v0002=SIX_ARGS)
    manager = MyFnManager("app", "0002")

    assert manager.get_forward_sql() == (
        f"DROP FUNCTION IF EXISTS my_fn({FOUR_SIGNATURE});\n{SIX_ARGS}"
    )
    assert manager.get_backward_sql() == (
        f"DROP FUNCTION IF EXISTS my_fn({SIX_SIGNATURE});\n{FOUR_ARGS}"
    )


def test_a_version_that_keeps_the_signature_runs_exactly_its_files(versions):
    versions(v0001=FOUR_ARGS, v0002=SIX_ARGS, v0003=SIX_ARGS_NEW_BODY)
    manager = MyFnManager("app", "0003")

    assert manager.get_forward_sql() == SIX_ARGS_NEW_BODY
    assert manager.get_backward_sql() == SIX_ARGS


def test_reversing_the_first_version_drops_each_signature_it_created(versions):
    versions(v0001=FOUR_ARGS + SIX_ARGS)

    assert MyFnManager("app", "0001").get_backward_sql() == (
        f"DROP FUNCTION IF EXISTS my_fn({FOUR_SIGNATURE});\n"
        f"DROP FUNCTION IF EXISTS my_fn({SIX_SIGNATURE});\n"
    )


def test_retiring_a_version_drops_its_signatures_and_going_back_creates_it_again(versions):
    versions(v0001=FOUR_ARGS, v0002=SIX_ARGS)

    retirement = MyFnManager("app", "0002").retirement()

    assert retirement.sql == f"DROP FUNCTION IF EXISTS my_fn({SIX_SIGNATURE});\n"
    assert retirement.reverse_sql == SIX_ARGS


def test_a_version_file_that_does_not_create_the_routine_is_refused(versions):
    versions(v0001="CREATE FUNCTION some_other_fn() RETURNS INT AS $$ SELECT 1 $$ LANGUAGE sql;")

    with pytest.raises(RawSQLSignatureError):
        MyFnManager("app", "0001").get_backward_sql()


def _routine_version_files() -> list[tuple[str, str, Path]]:
    return [
        (keyword, path.parent.name, path)
        for keyword, directory in (("FUNCTION", "functions"), ("PROCEDURE", "procedures"))
        for path in sorted(REPO_ROOT.glob(f"*/migrations/sql/{directory}/*/*.sql"))
    ]


@pytest.mark.parametrize(
    ("keyword", "name", "path"),
    _routine_version_files(),
    ids=lambda value: value.relative_to(REPO_ROOT).as_posix() if isinstance(value, Path) else "",
)
def test_every_version_file_in_the_repo_creates_the_routine_it_is_named_for(keyword, name, path):
    """Every migration built from these files reads their signatures, so a file the
    scanner cannot read would break ``migrate``."""
    assert routine_signatures(path.read_text(), keyword, name)


def test_procedures_get_the_same_treatment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "app" / "migrations" / "sql" / "procedures" / "my_proc"
    directory.mkdir(parents=True)
    (directory / "0001.sql").write_text(
        "CREATE PROCEDURE my_proc(p_a INT) LANGUAGE sql AS $$ SELECT 1 $$;\n"
    )
    (directory / "0002.sql").write_text(
        "CREATE PROCEDURE my_proc(p_a INT, p_b INT) LANGUAGE sql AS $$ SELECT 1 $$;\n"
    )

    class MyProcManager(ProcedureMigrationManager):
        name = "my_proc"

    assert (
        MyProcManager("app", "0002")
        .get_backward_sql()
        .startswith("DROP PROCEDURE IF EXISTS my_proc(p_a INT, p_b INT);\n")
    )

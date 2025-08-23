from typing import Literal

from django.db import migrations


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

    def get_forward_sql(self) -> str:
        with open(
            f"./{self.app_path}/migrations/sql/{self.dir_name()}/{self.name}/{self.version}.sql"
        ) as migration_file:
            return migration_file.read()

    def get_backward_sql(self) -> str:
        previous_version_int = int(self.version) - 1
        if previous_version_int <= 0:
            return self.drop_command_template.format(name=self.name)
        four_digits_previous_version = f"{previous_version_int:04d}"
        with open(
            f"./{self.app_path}/migrations/sql/{self.dir_name()}/{self.name}/{four_digits_previous_version}.sql"
        ) as migration_file:
            return migration_file.read()

    def migration(self):
        forward_sql = self.get_forward_sql()
        backward_sql = self.get_backward_sql()
        return migrations.RunSQL(sql=forward_sql, reverse_sql=backward_sql)


class FunctionMigrationManager(BaseRawSQLMigrationManager):
    migration_type = "function"
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


class ProcedureMigrationManager(BaseRawSQLMigrationManager):
    migration_type = "procedure"
    drop_command_template = "DROP PROCEDURE IF EXISTS {name};"

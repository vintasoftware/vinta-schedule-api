"""Runner for the 2026-08-05 untruncated-recurring-parent repair.

The script body, its safety contract, and the operator runbook live in
``scripts/one_off/2026-08-05-repair-untruncated-recurring-parents/``.
"""

from scripts.one_off._runtime_django import DjangoMgmtCommand, load_script_module


_script = load_script_module("2026-08-05-repair-untruncated-recurring-parents")


class Command(DjangoMgmtCommand):
    script_cls = _script.RepairUntruncatedRecurringParents
    config_builder = staticmethod(_script.build_config)

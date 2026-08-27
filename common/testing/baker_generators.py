"""Value generators model-bakery uses for this project's custom model fields.

``model_bakery`` resolves a generator by the field's *exact* class
(``field.__class__ in self.type_mapping``), not by ``isinstance``. A subclass of a
field it already knows is therefore unknown to it, and ``baker.make()`` on any model
carrying one raises ``TypeError: field <name> type <class> is not supported by baker``.

Register each custom field here and wire it up in ``BAKER_CUSTOM_FIELDS_GEN``
(``vinta_schedule_api/settings/base.py``), which takes dotted paths -- so nothing in
this module is imported unless model-bakery is installed and actually baking.
"""

import datetime

from django.utils import timezone


def gen_naive_datetime() -> datetime.datetime:
    """A naive "now", for ``common.fields.NaiveDateTimeField``.

    Same instant model-bakery's stock ``gen_datetime`` produced when these columns
    were plain ``DateTimeField``s -- ``timezone.now()`` is UTC and the column stores
    UTC -- with the tzinfo stripped, which is the contract the field's writers follow.
    """
    return timezone.now().replace(tzinfo=None)

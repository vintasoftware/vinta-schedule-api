from collections.abc import Callable
from typing import TypeVar

from django.core.exceptions import ObjectDoesNotExist
from django.db import models

from cuid2 import cuid_wrapper


def generate_unique_id():
    cuid_generator: Callable[[], str] = cuid_wrapper()
    return cuid_generator()


AnyModel = TypeVar("AnyModel", bound=models.Model)


def clone_model_instance(instance: AnyModel, save=True, **kwargs) -> AnyModel:
    new_instance = instance.__class__()
    for field in instance._meta.get_fields(include_hidden=False):
        # import ipdb

        # ipdb.set_trace()
        if field.is_relation and (field.many_to_many or field.one_to_many):
            continue
        try:
            original_value = getattr(instance, field.name)
            setattr(new_instance, field.name, kwargs.get(field.name, original_value))
        except ObjectDoesNotExist:
            pass

    new_instance.pk = None
    if save:
        new_instance.save()
    return new_instance

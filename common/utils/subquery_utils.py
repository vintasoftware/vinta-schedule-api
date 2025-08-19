from django.contrib.postgres.fields import ArrayField
from django.db import models


class SubqueryCount(models.Subquery):
    # Custom Count function to just perform simple count on any queryset without grouping.
    # https://stackoverflow.com/a/47371514/1164966
    template = "(SELECT count(*) FROM (%(subquery)s) _count)"
    output_field = models.PositiveIntegerField()


class SubqueryAggregate(models.Subquery):
    # https://code.djangoproject.com/ticket/10060
    template = '(SELECT %(function)s(_agg."%(column)s") FROM (%(subquery)s) _agg)'

    def __init__(self, queryset, column, output_field=None, **extra):
        if not output_field:
            # infer output_field from field type
            output_field = getattr(self, "output_field", queryset.model._meta.get_field(column))
        super().__init__(
            queryset.values(column), output_field, column=column, function=self.function, **extra
        )


class SubquerySum(SubqueryAggregate):
    function = "SUM"
    output_field = models.FloatField()


class SubqueryAvg(SubqueryAggregate):
    function = "AVG"
    output_field = models.FloatField()


class SubqueryMax(SubqueryAggregate):
    function = "MAX"


class SubqueryMin(SubqueryAggregate):
    function = "MIN"


class SubqueryJSON(SubqueryAggregate):
    function = "row_to_json"
    output_field = models.JSONField()


class SubqueryArray(models.Subquery):
    template = "(array(%(subquery)s))"

    def __init__(self, subquery, *args, **kwargs):
        self.output_field = ArrayField(  # type:ignore[misc]
            base_field=kwargs.pop("base_field", models.CharField())
        )
        array_variable_name = kwargs.pop("array_variable_name", "_array")
        super().__init__(subquery, *args, **kwargs)
        if hasattr(self, "extra") and isinstance(self.extra, dict):
            self.extra["array_variable_name"] = array_variable_name

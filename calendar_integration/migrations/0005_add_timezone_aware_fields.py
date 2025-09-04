# Add timezone-aware computed fields using GeneratedField

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('calendar_integration', '0004_add_recurring_models_db_functions'),
    ]

    operations = [
        # Add GeneratedField for timezone-aware start_time and end_time
        migrations.AddField(
            model_name='calendarevent',
            name='start_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('start_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
        migrations.AddField(
            model_name='calendarevent',
            name='end_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('end_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
        migrations.AddField(
            model_name='blockedtime',
            name='start_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('start_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
        migrations.AddField(
            model_name='blockedtime',
            name='end_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('end_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
        migrations.AddField(
            model_name='availabletime',
            name='start_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('start_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
        migrations.AddField(
            model_name='availabletime',
            name='end_time',
            field=models.GeneratedField(
                expression=models.Func(
                    models.F('end_time_tz_unaware'),
                    models.F('timezone'),
                    function='convert_naive_utc_to_timezone',
                    output_field=models.DateTimeField()
                ),
                output_field=models.DateTimeField(),
                db_persist=True
            ),
        ),
    ]

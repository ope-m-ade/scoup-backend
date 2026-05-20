from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academic", "0013_add_submitter_fields_to_ticket"),
    ]

    operations = [
        migrations.AddField(
            model_name="faculty",
            name="show_email_publicly",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="faculty",
            name="show_phone_publicly",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="faculty",
            name="allow_messages_through_scoup",
            field=models.BooleanField(default=True),
        ),
    ]

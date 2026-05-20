from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('academic', '0016_add_contact_documentation_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='contactpagesettings',
            name='backend_github_url',
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name='contactpagesettings',
            name='documentation_links',
            field=models.JSONField(blank=True, default=list),
        ),
    ]

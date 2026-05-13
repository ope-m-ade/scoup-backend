from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("academic", "0009_add_admin_audit_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="allow_student_interest",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="project",
            name="collaboration_invitation",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="project",
            name="is_open_to_collaboration",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="collaborationinquiry",
            name="requester_role",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="collaborationinquiry",
            name="target_project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="collaboration_inquiries",
                to="academic.project",
            ),
        ),
        migrations.AddField(
            model_name="collaborationinquiry",
            name="target_project_title",
            field=models.CharField(blank=True, max_length=300),
        ),
    ]

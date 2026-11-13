from django.db import migrations, models


class Migration(migrations.Migration):
    """Uploads of daily files record their day (YYYY-MM-DD), not only a month."""

    dependencies = [
        ("pestwatch", "0005_composites_pipeline"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inputupload",
            name="month",
            field=models.CharField(blank=True, help_text="YYYY-MM, or YYYY-MM-DD for a daily file", max_length=10),
        ),
    ]

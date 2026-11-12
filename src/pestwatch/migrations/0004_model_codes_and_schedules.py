"""Link the seeded pests to the risk models' codes, add the two modelled pests that were missing,
and create the automatic schedules."""
from django.db import migrations

CODES = {"faw": "FAW", "aphids": "APHIDS", "thrips": "THRIPS", "mlnd": "MLND"}
NEW_PESTS = [
    ("stemborer", "Stem borer", "insect", 5, "STEMBORER"),
    ("aaw", "African armyworm", "insect", 6, "AAW"),
]
SCHEDULES = ["weather", "nowcast", "forecast"]


def forwards(apps, schema_editor):
    Pest = apps.get_model("pestwatch", "Pest")
    for slug, code in CODES.items():
        Pest.objects.filter(slug=slug, model_code="").update(model_code=code)
    for slug, name, kind, order, code in NEW_PESTS:
        Pest.objects.get_or_create(slug=slug, defaults={"name": name, "kind": kind, "display_order": order,
                                                        "model_code": code})
    Schedule = apps.get_model("pestwatch", "PipelineSchedule")
    for p in SCHEDULES:
        Schedule.objects.get_or_create(pipeline=p)


def backwards(apps, schema_editor):
    Pest = apps.get_model("pestwatch", "Pest")
    Pest.objects.filter(slug__in=[p[0] for p in NEW_PESTS], pestlayer__isnull=True).delete()
    Pest.objects.filter(slug__in=list(CODES)).update(model_code="")


class Migration(migrations.Migration):
    dependencies = [("pestwatch", "0003_pipeline")]
    operations = [migrations.RunPython(forwards, backwards)]

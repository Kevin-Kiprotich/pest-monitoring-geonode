from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from geonode.layers.models import Dataset

from .common import TimeStampedModel


class Pest(TimeStampedModel):
    KIND_CHOICES = [
        ("insect", "Insect"),
        ("disease", "Disease"),
    ]
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    description = models.TextField(blank=True)
    display_order = models.PositiveIntegerField(default=0)
    # The pest's code in the risk models (FAW, APHIDS, THRIPS, STEMBORER, AAW, MLND).
    model_code = models.CharField(max_length=20, blank=True,
                                  help_text="Code the risk models use for this pest, e.g. FAW. Blank = not modelled.")

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name


class ProductType(models.TextChoices):
    HYBRID = "hybrid", "Hybrid (0-1)"
    CLASSIFIED = "classified", "Classified"


class TemporalType(models.TextChoices):
    BASELINE = "baseline", "Baseline"
    FORECAST = "forecast", "Forecast"
    NOWCAST = "nowcast", "Nowcast"


class PestStyle(TimeStampedModel):
    """An SLD shared by every layer of a product type.

    The SLD lives in GeoServer (in the default workspace) under ``name``;
    ``sld_body`` keeps a copy so the style can be recreated if it goes missing.
    """
    name = models.SlugField(unique=True)
    title = models.CharField(max_length=100)
    product_type = models.CharField(max_length=20, choices=ProductType.choices)
    sld_body = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        ordering = ["product_type", "title"]

    def __str__(self):
        return self.title


class LayerStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class PestLayer(TimeStampedModel):
    pest = models.ForeignKey(Pest, on_delete=models.PROTECT)
    product_type = models.CharField(max_length=20, choices=ProductType.choices)
    temporal_type = models.CharField(max_length=20, choices=TemporalType.choices, default=TemporalType.BASELINE)
    county = models.ForeignKey(
        "pestwatch.AdminBoundary", on_delete=models.PROTECT, related_name="pest_layers",
        limit_choices_to={"level": "county"})
    # Set once the raster has been published to GeoServer/GeoNode.
    dataset = models.OneToOneField(Dataset, on_delete=models.CASCADE, null=True, blank=True)
    style = models.ForeignKey(PestStyle, on_delete=models.PROTECT)
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    is_published = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=LayerStatus.choices, default=LayerStatus.PENDING)
    status_message = models.TextField(blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    # Path of the uploaded file while it waits to be published.
    staged_path = models.CharField(max_length=1024, blank=True)
    # Raster extent in EPSG:4326, used for the county check and to zoom the map.
    footprint = gis_models.PolygonField(srid=4326, null=True, blank=True)
    # "model" for layers the pipeline produced (run = the run that made it), "upload" otherwise.
    source = models.CharField(max_length=20, choices=[("upload", "Uploaded"), ("model", "Model run")], default="upload")
    run = models.ForeignKey("pestwatch.PipelineRun", on_delete=models.SET_NULL, null=True, blank=True,
                            related_name="layers")
    # Forecasts: the month the forecast was issued (first day of the month).
    issued_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"{self.pest} {self.get_product_type_display()} ({self.county})"

    @property
    def is_visible(self):
        return self.is_published and self.status == LayerStatus.READY and self.dataset_id is not None

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from geonode.layers.models import Dataset

from .common import TimeStampedModel


class AdminLevel(models.TextChoices):
    COUNTY = "county", "County"
    CONSTITUENCY = "constituency", "Constituency"
    WARD = "ward", "Ward"


# The level each level's parent belongs to.
PARENT_LEVEL = {
    AdminLevel.CONSTITUENCY: AdminLevel.COUNTY,
    AdminLevel.WARD: AdminLevel.CONSTITUENCY,
}


class BoundarySource(TimeStampedModel):
    """A GeoNode vector dataset whose features were imported as boundaries."""
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, null=True, blank=True)
    dataset_name = models.CharField(max_length=255)
    level = models.CharField(max_length=20, choices=AdminLevel.choices)
    name_field = models.CharField(max_length=100)
    code_field = models.CharField(max_length=100, blank=True)
    parent_field = models.CharField(
        max_length=100, blank=True,
        help_text="Attribute holding the parent's name. Left blank, parents are found spatially.")
    limit_values = models.TextField(
        blank=True,
        help_text="Comma-separated names to keep (counties for a county or constituency import, "
                  "constituencies for a ward import). Blank keeps everything.")
    feature_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.dataset_name} ({self.get_level_display()})"

    def limit_set(self):
        return {v.strip().lower() for v in self.limit_values.split(",") if v.strip()}


class AdminBoundary(TimeStampedModel):
    level = models.CharField(max_length=20, choices=AdminLevel.choices, db_index=True)
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100, blank=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
    source = models.ForeignKey(BoundarySource, on_delete=models.CASCADE, related_name="boundaries")
    geom = gis_models.MultiPolygonField(srid=4326)

    class Meta:
        ordering = ["level", "name"]
        constraints = [
            models.UniqueConstraint(fields=["level", "name", "parent"], name="pw_boundary_unique_in_parent"),
            models.UniqueConstraint(fields=["level", "name"], condition=models.Q(parent__isnull=True),
                                    name="pw_boundary_unique_root"),
        ]

    def __str__(self):
        return self.name

    @property
    def county(self):
        node = self
        while node is not None and node.level != AdminLevel.COUNTY:
            node = node.parent
        return node

    @classmethod
    def in_county(cls, county, level):
        """Boundaries of ``level`` that sit inside ``county``."""
        if level == AdminLevel.COUNTY:
            return cls.objects.filter(pk=county.pk)
        if level == AdminLevel.CONSTITUENCY:
            return cls.objects.filter(level=level, parent=county)
        return cls.objects.filter(level=level, parent__parent=county)

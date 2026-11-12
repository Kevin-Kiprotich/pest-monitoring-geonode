from django.db import models

from .common import TimeStampedModel


class ZonalStatistic(TimeStampedModel):
    """Summary of one raster inside one boundary.

    ``distribution`` maps a class value (classified) or a 0.2-wide bin
    such as "0.0-0.2" (hybrid) to a pixel count.
    """
    layer = models.ForeignKey("pestwatch.PestLayer", on_delete=models.CASCADE, related_name="zonal_stats")
    boundary = models.ForeignKey("pestwatch.AdminBoundary", on_delete=models.CASCADE, related_name="zonal_stats")
    pixel_count = models.PositiveIntegerField(default=0)
    valid_count = models.PositiveIntegerField(default=0)
    mean = models.FloatField(null=True, blank=True)
    minimum = models.FloatField(null=True, blank=True)
    maximum = models.FloatField(null=True, blank=True)
    std = models.FloatField(null=True, blank=True)
    distribution = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["layer", "boundary"], name="pw_zonal_unique"),
        ]

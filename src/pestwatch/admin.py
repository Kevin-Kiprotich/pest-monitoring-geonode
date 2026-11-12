from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import AdminBoundary, BoundarySource, Pest, PestLayer, PestStyle, ZonalStatistic


@admin.register(Pest)
class PestAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "kind", "display_order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(PestStyle)
class PestStyleAdmin(admin.ModelAdmin):
    list_display = ("title", "name", "product_type", "updated_at")
    list_filter = ("product_type",)


@admin.register(PestLayer)
class PestLayerAdmin(GISModelAdmin):
    list_display = ("__str__", "pest", "product_type", "county", "status", "is_published", "created_at")
    list_filter = ("status", "is_published", "product_type", "pest", "county")
    raw_id_fields = ("dataset",)
    readonly_fields = ("status", "status_message", "staged_path", "source_filename", "file_size")


@admin.register(BoundarySource)
class BoundarySourceAdmin(admin.ModelAdmin):
    list_display = ("dataset_name", "level", "name_field", "parent_field", "feature_count", "updated_at")


@admin.register(AdminBoundary)
class AdminBoundaryAdmin(GISModelAdmin):
    list_display = ("name", "level", "parent", "code")
    list_filter = ("level",)
    search_fields = ("name", "code")
    raw_id_fields = ("parent",)


@admin.register(ZonalStatistic)
class ZonalStatisticAdmin(admin.ModelAdmin):
    list_display = ("layer", "boundary", "mean", "valid_count")
    raw_id_fields = ("layer", "boundary")

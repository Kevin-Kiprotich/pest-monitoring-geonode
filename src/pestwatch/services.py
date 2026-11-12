"""PestWatch workflows: staging, publishing, deleting layers and computing statistics."""
import logging
import os
import shutil
import uuid

from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction
from django.utils.text import slugify
from osgeo import gdal, osr

from . import conf, geoserver
from .models import AdminBoundary, AdminLevel, LayerStatus, PestLayer, ProductType, ZonalStatistic
from .raster import inspect_raster, validate_product, values_to_nodata
from .zonal import zonal_stats

logger = logging.getLogger(__name__)

STAT_LEVELS = (AdminLevel.COUNTY, AdminLevel.CONSTITUENCY, AdminLevel.WARD)


def staging_dir():
    path = conf.get("STAGING_DIR")
    os.makedirs(path, exist_ok=True)
    return path


def stage_upload(uploaded_file):
    """Copy an uploaded file into the staging directory and return its path."""
    path = os.path.join(staging_dir(), f"{uuid.uuid4().hex}.tif")
    if hasattr(uploaded_file, "temporary_file_path"):
        shutil.copyfile(uploaded_file.temporary_file_path(), path)
    else:
        with open(path, "wb") as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)
    return path


def _remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            logger.exception("Could not remove %s", path)


def default_title(layer):
    title = f"{layer.pest.name} – {layer.get_product_type_display()} – {layer.county.name}"
    if layer.valid_from:
        title += f" ({layer.valid_from:%b %Y})"
    return title


def layer_name(layer):
    """A unique GeoServer layer name, e.g. pw_faw_classified_kitui_1a2b3c4d."""
    county = slugify(layer.county.name).replace("-", "_")[:20]
    return f"pw_{layer.pest.slug.replace('-', '_')}_{layer.product_type}_{county}_{uuid.uuid4().hex[:8]}"


def keywords(layer):
    return ["pestwatch", layer.pest.slug, layer.product_type, layer.temporal_type, slugify(layer.county.name)]


def _to_cog(src, dst):
    """Write ``src`` as a COG in PUBLISH_SRS, reprojecting only when its CRS differs."""
    options = ["COMPRESS=DEFLATE", "OVERVIEWS=AUTO"]
    target = osr.SpatialReference()
    target.SetFromUserInput(conf.get("PUBLISH_SRS"))
    source = gdal.Open(src).GetSpatialRef()
    if source is not None and source.IsSame(target):
        gdal.Translate(dst, src, format="COG", creationOptions=options)
    else:
        # Nearest neighbour keeps class values and nodata exact.
        gdal.Warp(dst, src, format="COG", dstSRS=conf.get("PUBLISH_SRS"), resampleAlg="near",
                  creationOptions=options)


def process_layer(layer_id):
    """Publish a staged raster and compute its statistics. Runs as a background job."""
    layer = PestLayer.objects.select_related("pest", "county", "style", "uploaded_by").get(pk=layer_id)
    if layer.status not in (LayerStatus.PENDING, LayerStatus.FAILED) or not layer.staged_path:
        logger.info("Layer %s is %s; nothing to process", layer.pk, layer.status)
        return layer
    PestLayer.objects.filter(pk=layer.pk).update(status=LayerStatus.PROCESSING, status_message="")

    name = layer_name(layer)
    cog_path = os.path.join(staging_dir(), f"{name}.tif")
    masked_path = None
    dataset = None
    try:
        info = inspect_raster(layer.staged_path)
        validate_product(info, layer.product_type)
        source = layer.staged_path
        no_data = set(conf.get("NO_DATA_CLASSES")) & set(info.values)
        if layer.product_type == ProductType.CLASSIFIED and no_data:
            # e.g. 0 = "no host" from the pest pipeline: publish it as nodata.
            masked_path = os.path.join(staging_dir(), f"{name}_masked.tif")
            source = values_to_nodata(layer.staged_path, masked_path, sorted(no_data))
        _to_cog(source, cog_path)

        dataset = geoserver.publish_raster(cog_path, name, owner=layer.uploaded_by)
        geoserver.apply_style(dataset, layer.style)
        layer.title = layer.title or default_title(layer)
        geoserver.update_metadata(dataset, layer.title, layer.description, keywords(layer))

        layer.dataset = dataset
        layer.footprint = GEOSGeometry(info.footprint_wkt, srid=4326)
        layer.save()
        compute_layer_stats(layer, raster_path=cog_path)

        layer.status = LayerStatus.READY
        layer.status_message = ""
        _remove(layer.staged_path)
        layer.staged_path = ""
        layer.save()
    except Exception as e:
        logger.exception("Publishing layer %s failed", layer.pk)
        if dataset is not None:
            # Unlink first: the dataset FK cascades and would delete this record.
            PestLayer.objects.filter(pk=layer.pk).update(dataset=None)
            try:
                geoserver.delete_dataset(dataset)
            except Exception:
                logger.exception("Rollback of dataset %s failed", dataset.pk)
        else:
            geoserver.delete_unregistered(name)
        PestLayer.objects.filter(pk=layer.pk).update(
            status=LayerStatus.FAILED, status_message=str(e)[:2000], dataset=None)
    finally:
        _remove(cog_path)
        _remove(masked_path)
    layer.refresh_from_db()
    return layer


def _zones(county):
    zones = []
    for level in STAT_LEVELS:
        for b in AdminBoundary.in_county(county, level).only("pk", "geom"):
            zones.append((b.pk, b.geom.wkb))
    return zones


def _download_raster(layer, path):
    """Fetch the published GeoTIFF from GeoServer (WCS) when the staged copy is gone."""
    import requests
    from django.conf import settings
    ogc = settings.OGC_SERVER["default"]
    response = requests.get(
        f"{ogc['LOCATION'].rstrip('/')}/ows",
        params={"service": "WCS", "version": "2.0.1", "request": "GetCoverage",
                "coverageId": layer.dataset.alternate.replace(":", "__"), "format": "image/tiff"},
        auth=(ogc["USER"], ogc["PASSWORD"]), stream=True, timeout=600)
    if response.status_code != 200 or "tiff" not in response.headers.get("Content-Type", ""):
        raise RuntimeError(f"Could not download {layer.dataset.alternate} from GeoServer "
                           f"(HTTP {response.status_code}).")
    with open(path, "wb") as out:
        shutil.copyfileobj(response.raw, out)


def compute_layer_stats(layer, raster_path=None):
    """(Re)compute the zonal statistics of a layer for every boundary in its county."""
    tmp = None
    if raster_path is None:
        tmp = os.path.join(staging_dir(), f"stats_{uuid.uuid4().hex}.tif")
        _download_raster(layer, tmp)
        raster_path = tmp
    try:
        results = zonal_stats(raster_path, _zones(layer.county), layer.product_type)
        with transaction.atomic():
            ZonalStatistic.objects.filter(layer=layer).delete()
            ZonalStatistic.objects.bulk_create(
                ZonalStatistic(layer=layer, boundary_id=key, **stats) for key, stats in results.items())
    finally:
        _remove(tmp)
    return len(results)


def recompute_county_stats(county_ids):
    for layer in PestLayer.objects.filter(county_id__in=county_ids, status=LayerStatus.READY,
                                          dataset__isnull=False).select_related("county", "dataset"):
        try:
            compute_layer_stats(layer)
        except Exception:
            logger.exception("Statistics for layer %s failed", layer.pk)


def update_layer(layer, changed_fields):
    """Push edits made to a ready layer to GeoNode/GeoServer."""
    if not layer.dataset_id:
        return
    if "style" in changed_fields:
        geoserver.apply_style(layer.dataset, layer.style)
    if changed_fields & {"title", "description", "pest", "county", "valid_from"}:
        layer.title = layer.title or default_title(layer)
        layer.save(update_fields=["title", "updated_at"])
        geoserver.update_metadata(layer.dataset, layer.title, layer.description, keywords(layer))


def delete_layer(layer):
    if layer.dataset_id:
        geoserver.delete_dataset(layer.dataset)
    _remove(layer.staged_path)
    PestLayer.objects.filter(pk=layer.pk).delete()

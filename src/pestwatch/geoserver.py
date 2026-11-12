"""Publishing rasters and shared styles through GeoNode's GeoServer helpers."""
import logging

from django.conf import settings
from lxml import etree

from geonode.geoserver.helpers import (
    _extract_style_version_from_sld, cascading_delete, gs_catalog, gs_slurp, set_styles)
from geonode.layers.models import Dataset
from geonode.resource.registry import dataset_manager

logger = logging.getLogger(__name__)

# GeoServer's built-in raster style, used to detach shared styles before deletion.
FALLBACK_STYLE = "raster"


def workspace():
    return settings.DEFAULT_WORKSPACE


def parse_sld(sld_body):
    """Raise ValueError unless ``sld_body`` is well-formed SLD XML."""
    data = sld_body.encode("utf-8") if isinstance(sld_body, str) else sld_body
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as e:
        raise ValueError(f"The SLD is not valid XML: {e}")
    if etree.QName(root).localname != "StyledLayerDescriptor":
        raise ValueError("The file is not an SLD (the root element must be StyledLayerDescriptor).")
    return data


def save_style(pest_style, overwrite=True):
    """Create or update the shared style in GeoServer from ``pest_style.sld_body``."""
    data = parse_sld(pest_style.sld_body)
    gs_catalog.create_style(
        pest_style.name, data, overwrite=overwrite, raw=True,
        style_format=_extract_style_version_from_sld(data), workspace=workspace())
    gs_catalog.reset()


def ensure_style(pest_style):
    """Return the GeoServer style, recreating it from the stored SLD if it is missing."""
    style = gs_catalog.get_style(pest_style.name, workspace=workspace())
    if style is None:
        save_style(pest_style, overwrite=False)
        style = gs_catalog.get_style(pest_style.name, workspace=workspace())
    if style is None:
        raise RuntimeError(f"Could not create the style {pest_style.name} in GeoServer.")
    return style


def delete_style(pest_style):
    style = gs_catalog.get_style(pest_style.name, workspace=workspace())
    if style is not None:
        gs_catalog.delete(style, purge=True, recurse=False)
        gs_catalog.reset()


def publish_raster(path, name, owner):
    """Upload a GeoTIFF to GeoServer and register it in GeoNode. Returns the Dataset."""
    # overwrite=True skips gsconfig's "does it exist?" check. That check caches an
    # empty store list for 5 s and, on the upload path, the cache is not cleared, so
    # the lookup after a fast upload finds nothing (IndexError). Names are unique.
    gs_catalog.create_coveragestore(
        name, workspace=workspace(), path=path, type="GeoTIFF",
        create_layer=True, layer_name=name, upload_data=True, overwrite=True)
    gs_catalog.reset()
    output = gs_slurp(
        workspace=workspace(), store=name, filter=name, owner=owner,
        ignore_errors=True, verbosity=0, execute_signals=True)
    failed = [r for r in output["layers"] if r.get("status") == "failed"]
    if failed:
        raise RuntimeError(f"GeoNode could not register the layer: {failed[0].get('error')}")
    dataset = Dataset.objects.filter(workspace=workspace(), name=name).first()
    if dataset is None:
        raise RuntimeError("GeoNode did not register the published layer.")
    return dataset


def apply_style(dataset, pest_style):
    """Make the shared style the dataset's default style, in GeoServer and GeoNode."""
    style = ensure_style(pest_style)
    gs_layer = gs_catalog.get_layer(dataset.alternate)
    if gs_layer is None:
        raise RuntimeError(f"Layer {dataset.alternate} not found in GeoServer.")
    gs_layer.default_style = style
    gs_layer.styles = []
    gs_catalog.save(gs_layer)
    gs_catalog.reset()
    set_styles(dataset, gs_catalog)
    _remove_layer_copy_style(dataset)


def _remove_layer_copy_style(dataset):
    """Delete the per-layer copy of the default style GeoNode makes on registration."""
    from geonode.layers.models import Style
    from .models import PestStyle
    if PestStyle.objects.filter(name=dataset.name).exists():
        return  # never a shared style
    try:
        copy = gs_catalog.get_style(dataset.name, workspace=workspace())
        if copy is not None:
            gs_catalog.delete(copy, purge=True, recurse=False)
            gs_catalog.reset()
    except Exception:
        logger.debug("No per-layer style to remove for %s", dataset.alternate, exc_info=True)
    Style.objects.filter(name=dataset.name, dataset_default_style__isnull=True).delete()


def _detach_shared_style(dataset):
    """Point the layer at GeoServer's built-in style so deleting it can't remove a shared style."""
    try:
        gs_layer = gs_catalog.get_layer(dataset.alternate)
        if gs_layer is not None:
            gs_layer.default_style = gs_catalog.get_style(FALLBACK_STYLE)
            gs_layer.styles = []
            gs_catalog.save(gs_layer)
            gs_catalog.reset()
    except Exception:
        logger.exception("Could not detach the style from %s", dataset.alternate)
    # GeoNode deletes the dataset's Style rows when the dataset is removed;
    # unlink them so the rows for shared styles survive.
    dataset.styles.clear()
    Dataset.objects.filter(pk=dataset.pk).update(default_style=None)
    dataset.default_style = None


def delete_dataset(dataset):
    _detach_shared_style(dataset)
    dataset_manager.delete(dataset.uuid, instance=dataset)


def delete_unregistered(name):
    """Remove a coverage store that never made it into GeoNode."""
    try:
        cascading_delete(dataset_name=f"{workspace()}:{name}", catalog=gs_catalog)
    except Exception:
        logger.exception("Could not remove %s from GeoServer", name)


def update_metadata(dataset, title, abstract, keywords):
    vals = {"title": title, "abstract": abstract}
    from geonode.base.models import TopicCategory
    category = TopicCategory.objects.filter(identifier="farming").first()
    if category:
        vals["category"] = category
    dataset_manager.update(dataset.uuid, instance=dataset, vals=vals, keywords=keywords, notify=False)

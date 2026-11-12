"""JSON endpoints used by the dashboard and the Layers tab."""
import csv

from django.contrib.auth.decorators import permission_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from geonode.layers.models import Dataset

from .. import conf
from ..boundaries import BoundaryImportError, dataset_fields
from ..models import AdminBoundary, AdminLevel, LayerStatus, Pest, PestLayer, ProductType, ZonalStatistic
from ..zonal import distribution_keys

SIMPLIFY = {AdminLevel.COUNTY: 0.001, AdminLevel.CONSTITUENCY: 0.0005, AdminLevel.WARD: 0.0002}


def visible_layers():
    return PestLayer.objects.filter(
        is_published=True, status=LayerStatus.READY, dataset__isnull=False
    ).select_related("pest", "county", "style", "dataset")


@require_GET
def catalog(request):
    counties = AdminBoundary.objects.filter(level=AdminLevel.COUNTY).order_by("name")
    qs = visible_layers()
    temporal = [t for t in request.GET.get("temporal", "").split(",") if t]
    if temporal:
        qs = qs.filter(temporal_type__in=temporal)
    # Forecasts: only months not yet over, the coming months first. Everything else: the most recent first.
    order = ("-valid_from", "-created_at")
    if temporal == ["forecast"]:
        qs = qs.filter(Q(valid_to__isnull=True) | Q(valid_to__gte=timezone.now().date()))
        order = ("valid_from", "-created_at")
    layers = []
    for layer in qs.order_by(*order):
        layers.append({
            "id": layer.pk,
            "title": str(layer),
            "pest": layer.pest.slug,
            "product": layer.product_type,
            "temporal": layer.temporal_type,
            "county": layer.county_id,
            "validFrom": layer.valid_from.isoformat() if layer.valid_from else None,
            "validTo": layer.valid_to.isoformat() if layer.valid_to else None,
            "issued": layer.issued_on.isoformat() if layer.issued_on else None,
            "source": layer.source,
            "wmsLayer": layer.dataset.alternate,
            "style": f"{layer.dataset.workspace}:{layer.style.name}",
            "bbox": layer.footprint.extent if layer.footprint else None,
            "datasetUrl": layer.dataset.detail_url,
        })
    return JsonResponse({
        "counties": [{"id": c.pk, "name": c.name, "bbox": c.geom.extent} for c in counties],
        "pests": [{"slug": p.slug, "name": p.name, "kind": p.kind} for p in Pest.objects.all()],
        "products": [{"value": v, "label": label} for v, label in ProductType.choices],
        "levels": [{"value": v, "label": label} for v, label in AdminLevel.choices if v != AdminLevel.COUNTY],
        "layers": layers,
    })


@require_GET
@cache_control(max_age=300)
def boundaries(request):
    try:
        county = AdminBoundary.objects.get(level=AdminLevel.COUNTY, pk=int(request.GET.get("county", "")))
    except (ValueError, AdminBoundary.DoesNotExist):
        return HttpResponseBadRequest("Unknown county")
    level = request.GET.get("level", AdminLevel.WARD)
    if level not in AdminLevel.values:
        return HttpResponseBadRequest("Unknown level")

    features = []
    for b in AdminBoundary.in_county(county, level).select_related("parent"):
        geom = b.geom.simplify(SIMPLIFY[level], preserve_topology=True)
        features.append({
            "type": "Feature",
            "id": b.pk,
            "properties": {"id": b.pk, "name": b.name, "code": b.code, "level": b.level,
                           "parent": b.parent.name if b.parent else None},
            "geometry": {"type": geom.geom_type, "coordinates": geom.coords},
        })
    return JsonResponse({"type": "FeatureCollection", "features": features})


def _high_share(stat, product_type):
    """Share of valid pixels in the top two classes (classified) or above 0.6 (hybrid)."""
    if not stat.valid_count:
        return None
    high = sum(stat.distribution.get(k, 0) for k in distribution_keys(product_type)[-2:])
    return high / stat.valid_count


def _stat_row(stat, product_type):
    b = stat.boundary
    return {
        "id": b.pk, "name": b.name, "code": b.code, "level": b.level,
        "parent": b.parent.name if b.parent else None,
        "mean": stat.mean, "minimum": stat.minimum, "maximum": stat.maximum, "std": stat.std,
        "pixelCount": stat.pixel_count, "validCount": stat.valid_count,
        "distribution": stat.distribution, "highShare": _high_share(stat, product_type),
    }


@require_GET
def stats(request):
    layer_id = request.GET.get("layer", "")
    if not layer_id.isdigit():
        return HttpResponseBadRequest("Unknown layer")
    layer = get_object_or_404(visible_layers(), pk=int(layer_id))
    level = request.GET.get("level", AdminLevel.WARD)
    if level not in AdminLevel.values:
        return HttpResponseBadRequest("Unknown level")

    qs = ZonalStatistic.objects.filter(layer=layer).select_related("boundary", "boundary__parent")
    county_stat = qs.filter(boundary=layer.county).first()
    units = [_stat_row(s, layer.product_type) for s in qs.filter(boundary__level=level)]
    units.sort(key=lambda u: (u["mean"] is None, -(u["mean"] or 0)))

    if request.GET.get("format") == "csv":
        return _stats_csv(layer, level, units)
    return JsonResponse({
        "layer": {"id": layer.pk, "title": str(layer), "product": layer.product_type,
                  "pest": layer.pest.name, "county": layer.county.name},
        "county": _stat_row(county_stat, layer.product_type) if county_stat else None,
        "level": level,
        "units": units,
    })


def _stats_csv(layer, level, units):
    response = HttpResponse(content_type="text/csv")
    filename = slugify(f"{layer} {level} statistics")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    keys = distribution_keys(layer.product_type)
    label = (lambda k: conf.get("CLASSES").get(int(k), k)) if layer.product_type == ProductType.CLASSIFIED \
        else (lambda k: k)
    writer = csv.writer(response)
    writer.writerow(["level", "name", "code", "parent", "mean", "min", "max", "std", "valid_pixels",
                     "high_risk_share"] + [f"pixels_{label(k)}" for k in keys])
    for u in units:
        writer.writerow([u["level"], u["name"], u["code"], u["parent"], u["mean"], u["minimum"], u["maximum"],
                         u["std"], u["validCount"], u["highShare"]] + [u["distribution"].get(k, 0) for k in keys])
    return response


@require_GET
@permission_required("pestwatch.view_pestlayer", raise_exception=True)
def layer_status(request):
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.isdigit()]
    rows = PestLayer.objects.filter(pk__in=ids).values("pk", "status", "status_message")
    return JsonResponse({str(r["pk"]): {"status": r["status"], "message": r["status_message"]} for r in rows})


@require_GET
@permission_required("pestwatch.add_boundarysource", raise_exception=True)
def dataset_attributes(request, pk):
    dataset = get_object_or_404(Dataset, pk=pk)
    try:
        return JsonResponse({"fields": dataset_fields(dataset)})
    except BoundaryImportError as e:
        return JsonResponse({"fields": [], "error": str(e)})

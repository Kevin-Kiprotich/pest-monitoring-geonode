"""The Layers tab: create, view, edit and delete rasters, boundaries and styles."""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import geoserver, jobs, services
from ..boundaries import BoundaryImportError, import_boundaries
from ..forms import BoundaryImportForm, PestLayerEditForm, PestStyleForm, RasterUploadForm
from ..models import AdminBoundary, AdminLevel, BoundarySource, LayerStatus, Pest, PestLayer, PestStyle, ProductType
from .pages import portal_context

logger = logging.getLogger(__name__)


def perm(codename):
    """Require login, then the pestwatch permission (403 if missing)."""
    def decorator(view):
        return login_required(permission_required(f"pestwatch.{codename}", raise_exception=True)(view))
    return decorator


def _render(request, template, tab, **context):
    return render(request, template, {**portal_context(request, "layers"), "tab": tab, **context})


# --- Rasters -----------------------------------------------------------------

@perm("view_pestlayer")
def layer_list(request):
    layers = PestLayer.objects.select_related("pest", "county", "style", "dataset", "uploaded_by") \
        .annotate(stat_count=Count("zonal_stats"))
    filters = {k: request.GET.get(k) for k in ("pest", "product", "county", "status") if request.GET.get(k)}
    if "pest" in filters:
        layers = layers.filter(pest__slug=filters["pest"])
    if "product" in filters:
        layers = layers.filter(product_type=filters["product"])
    if "county" in filters:
        layers = layers.filter(county_id=filters["county"])
    if "status" in filters:
        layers = layers.filter(status=filters["status"])
    return _render(
        request, "pestwatch/layers/list.html", "rasters",
        layers=layers, filters=filters, pests=Pest.objects.all(), products=ProductType.choices,
        counties=AdminBoundary.objects.filter(level=AdminLevel.COUNTY), statuses=LayerStatus.choices,
        busy_ids=[layer.pk for layer in layers if layer.status in (LayerStatus.PENDING, LayerStatus.PROCESSING)])


@perm("add_pestlayer")
def layer_upload(request):
    if not AdminBoundary.objects.filter(level=AdminLevel.COUNTY).exists():
        messages.warning(request, "Import the county boundaries (or constituencies with a county field) "
                                  "before uploading rasters.")
        return redirect("pestwatch:boundary_list")
    form = RasterUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            layer = form.save(request.user)
        except Exception as e:
            logger.exception("Saving the upload failed")
            messages.error(request, f"The upload could not be saved: {e}")
        else:
            jobs.process_layer(layer.pk)
            messages.success(request, f"{layer.source_filename} passed validation and is being published. "
                                      f"This page updates when it is ready.")
            return redirect("pestwatch:layer_list")
    styles = {s.pk: s.product_type for s in PestStyle.objects.all()}
    return _render(request, "pestwatch/layers/upload.html", "rasters", form=form, style_products=styles)


@perm("view_pestlayer")
def layer_detail(request, pk):
    layer = get_object_or_404(PestLayer.objects.select_related("pest", "county", "style", "dataset"), pk=pk)
    stats = layer.zonal_stats.select_related("boundary", "boundary__parent").order_by("boundary__level",
                                                                                      "boundary__name")
    return _render(request, "pestwatch/layers/detail.html", "rasters", layer=layer, stats=stats)


@perm("change_pestlayer")
def layer_edit(request, pk):
    layer = get_object_or_404(PestLayer.objects.select_related("pest", "county", "style", "dataset"), pk=pk)
    form = PestLayerEditForm(request.POST or None, instance=layer)
    if request.method == "POST" and form.is_valid():
        changed = set(form.changed_data)
        layer = form.save()
        try:
            services.update_layer(layer, changed)
        except Exception as e:
            logger.exception("Updating layer %s in GeoNode failed", layer.pk)
            messages.error(request, f"Saved, but GeoNode/GeoServer could not be updated: {e}")
        if "county" in changed and layer.status == LayerStatus.READY:
            jobs.recompute_layer(layer.pk)
            messages.info(request, "Statistics are being recomputed for the new county.")
        messages.success(request, f"Saved {layer}.")
        return redirect("pestwatch:layer_list")
    return _render(request, "pestwatch/layers/edit.html", "rasters", form=form, layer=layer)


@perm("change_pestlayer")
@require_POST
def layer_recompute(request, pk):
    layer = get_object_or_404(PestLayer, pk=pk, status=LayerStatus.READY)
    jobs.recompute_layer(layer.pk)
    messages.info(request, f"Statistics for {layer} are being recomputed.")
    return redirect("pestwatch:layer_detail", pk=layer.pk)


@perm("change_pestlayer")
@require_POST
def layer_toggle_publish(request, pk):
    layer = get_object_or_404(PestLayer, pk=pk)
    layer.is_published = not layer.is_published
    layer.save(update_fields=["is_published", "updated_at"])
    messages.success(request, f"{layer} is now {'shown on' if layer.is_published else 'hidden from'} the dashboard.")
    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("pestwatch:layer_list")


@perm("delete_pestlayer")
def layer_delete(request, pk):
    layer = get_object_or_404(PestLayer.objects.select_related("dataset"), pk=pk)
    if request.method == "POST":
        name = str(layer)
        try:
            services.delete_layer(layer)
        except Exception as e:
            logger.exception("Deleting layer %s failed", pk)
            messages.error(request, f"{name} could not be deleted: {e}")
        else:
            messages.success(request, f"Deleted {name}.")
        return redirect("pestwatch:layer_list")
    return _render(request, "pestwatch/layers/confirm_delete.html", "rasters", object=layer,
                   cancel_url="pestwatch:layer_list",
                   consequences=["The GeoNode dataset and its GeoServer layer are removed.",
                                 "Its zonal statistics are deleted."])


# --- Boundaries --------------------------------------------------------------

@perm("view_boundarysource")
def boundary_list(request):
    sources = BoundarySource.objects.select_related("dataset").annotate(n=Count("boundaries")).order_by("level")
    counties = AdminBoundary.objects.filter(level=AdminLevel.COUNTY).annotate(
        constituencies=Count("children", distinct=True), layers=Count("pest_layers", distinct=True))
    ward_counts = {c.pk: AdminBoundary.in_county(c, AdminLevel.WARD).count() for c in counties}
    for c in counties:
        c.wards = ward_counts[c.pk]
    return _render(request, "pestwatch/boundaries/list.html", "boundaries", sources=sources, counties=counties)


@perm("add_boundarysource")
def boundary_import(request):
    form = BoundaryImportForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source = form.save(request.user)
        return _run_import(request, source, form.cleaned_data["dataset"])
    return _render(request, "pestwatch/boundaries/import.html", "boundaries", form=form)


def _run_import(request, source, dataset):
    try:
        counties = import_boundaries(source, dataset)
    except BoundaryImportError as e:
        if not source.boundaries.exists():
            source.delete()
        messages.error(request, str(e))
        return redirect("pestwatch:boundary_list")
    msg = f"Imported {source.feature_count} {source.get_level_display().lower()} boundaries from {dataset.title}."
    if source.unmatched_count:
        msg += f" {source.unmatched_count} were skipped because they fall outside every known parent boundary."
    messages.success(request, msg)
    if PestLayer.objects.filter(county__in=counties, status=LayerStatus.READY).exists():
        jobs.recompute_counties([c.pk for c in counties])
        messages.info(request, "Statistics for the affected counties are being recomputed.")
    return redirect("pestwatch:boundary_list")


@perm("change_boundarysource")
@require_POST
def boundary_reimport(request, pk):
    source = get_object_or_404(BoundarySource, pk=pk)
    if source.dataset is None:
        messages.error(request, f"The dataset {source.dataset_name} no longer exists in GeoNode.")
        return redirect("pestwatch:boundary_list")
    return _run_import(request, source, source.dataset)


@perm("delete_boundarysource")
def boundary_delete(request, pk):
    source = get_object_or_404(BoundarySource, pk=pk)
    if request.method == "POST":
        try:
            source.delete()
        except ProtectedError:
            messages.error(request, "Rasters still use counties from this import. Delete those rasters first.")
        else:
            messages.success(request, f"Removed the boundaries imported from {source.dataset_name}.")
        return redirect("pestwatch:boundary_list")
    return _render(request, "pestwatch/layers/confirm_delete.html", "boundaries", object=source,
                   cancel_url="pestwatch:boundary_list",
                   consequences=[f"{source.boundaries.count()} boundaries (and anything beneath them) are removed "
                                 "from PestWatch, with their zonal statistics.",
                                 "The GeoNode dataset itself is not touched."])


# --- Styles ------------------------------------------------------------------

@perm("view_peststyle")
def style_list(request):
    styles = PestStyle.objects.annotate(n=Count("pestlayer"))
    return _render(request, "pestwatch/styles/list.html", "styles", styles=styles)


@perm("add_peststyle")
def style_create(request):
    return _style_form(request, PestStyle())


@perm("change_peststyle")
def style_edit(request, pk):
    return _style_form(request, get_object_or_404(PestStyle, pk=pk))


def _style_form(request, style):
    form = PestStyleForm(request.POST or None, request.FILES or None, instance=style)
    if request.method == "POST" and form.is_valid():
        style = form.save(commit=False)
        style.sld_body = form.cleaned_data["sld_body"]
        if not style.pk:
            style.created_by = request.user
        style.save()
        try:
            geoserver.save_style(style)
        except Exception as e:
            logger.exception("Saving style %s to GeoServer failed", style.name)
            messages.error(request, f"Saved, but GeoServer rejected the style: {e}")
        else:
            messages.success(request, f"Saved style {style}. Layers using it update immediately.")
        return redirect("pestwatch:style_list")
    return _render(request, "pestwatch/styles/form.html", "styles", form=form, style=style)


@perm("delete_peststyle")
def style_delete(request, pk):
    style = get_object_or_404(PestStyle, pk=pk)
    if request.method == "POST":
        try:
            style.delete()
        except ProtectedError:
            messages.error(request, f"{style} is used by rasters; change their style first.")
        else:
            try:
                geoserver.delete_style(style)
            except Exception:
                logger.exception("Could not delete style %s from GeoServer", style.name)
            messages.success(request, f"Deleted style {style}.")
        return redirect("pestwatch:style_list")
    return _render(request, "pestwatch/layers/confirm_delete.html", "styles", object=style,
                   cancel_url="pestwatch:style_list",
                   consequences=["The style is removed from GeoServer."])

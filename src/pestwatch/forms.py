import os
import re

from django import forms
from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction
from django.template.defaultfilters import filesizeformat

from geonode.layers.models import Dataset

from . import conf, geoserver, services
from .boundaries import BoundaryImportError, dataset_fields
from .models import (
    PARENT_LEVEL, AdminBoundary, AdminLevel, BoundarySource, PestLayer, PestStyle, Pipeline, TemporalType)
from .raster import RasterError, check_county, inspect_raster, validate_product


class BootstrapMixin:
    """Add Bootstrap 3 classes to widgets."""

    def _bootstrap(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.ClearableFileInput, forms.FileInput, forms.RadioSelect,
                                   forms.CheckboxSelectMultiple)):
                continue
            widget.attrs.setdefault("class", "form-control")


class DateInput(forms.DateInput):
    input_type = "date"


def counties():
    return AdminBoundary.objects.filter(level=AdminLevel.COUNTY).order_by("name")


class RasterUploadForm(BootstrapMixin, forms.ModelForm):
    raster = forms.FileField(help_text="Single-band GeoTIFF with a nodata value.")
    existing_style = forms.ModelChoiceField(
        queryset=PestStyle.objects.all(), required=False, label="Style",
        help_text="Choose a saved style, or upload a new SLD below.")
    new_sld = forms.FileField(required=False, label="New SLD file")
    new_style_name = forms.SlugField(required=False, help_text="Short identifier, e.g. faw-classified-v2.")
    new_style_title = forms.CharField(required=False, max_length=100)

    class Meta:
        model = PestLayer
        fields = ["pest", "product_type", "county", "valid_from", "valid_to", "title", "description",
                  "is_published"]
        widgets = {"valid_from": DateInput(), "valid_to": DateInput(),
                   "description": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_published": "Show on the dashboard when ready"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["county"].queryset = counties()
        self.fields["title"].help_text = "Leave blank to generate one."
        self.fields["is_published"].initial = True
        self.fields["raster"].help_text += f" Maximum {filesizeformat(conf.get('MAX_RASTER_BYTES'))}."
        self.fields["raster"].widget.attrs["accept"] = ".tif,.tiff,image/tiff"
        self.fields["new_sld"].widget.attrs["accept"] = ".sld,.xml"
        self.raster_info = None
        self._bootstrap()

    def clean_raster(self):
        f = self.cleaned_data["raster"]
        limit = conf.get("MAX_RASTER_BYTES")
        if f.size > limit:
            raise forms.ValidationError(
                f"The file is {filesizeformat(f.size)}; the limit is {filesizeformat(limit)}.")
        if os.path.splitext(f.name)[1].lower() not in {".tif", ".tiff"}:
            raise forms.ValidationError("Upload a GeoTIFF (.tif or .tiff).")
        return f

    def clean_new_style_name(self):
        name = self.cleaned_data.get("new_style_name")
        if name and PestStyle.objects.filter(name=name).exists():
            raise forms.ValidationError("A style with this name already exists; choose it from the list instead.")
        return name

    def clean(self):
        data = super().clean()
        product = data.get("product_type")
        existing, new_sld = data.get("existing_style"), data.get("new_sld")

        if bool(existing) == bool(new_sld):
            self.add_error("existing_style", "Choose a saved style or upload a new SLD (not both).")
        elif existing and product and existing.product_type != product:
            self.add_error("existing_style", f"{existing} is for {existing.get_product_type_display()} rasters.")
        elif new_sld:
            if not data.get("new_style_name"):
                self.add_error("new_style_name", "Give the new style a name.")
            try:
                data["new_sld_body"] = geoserver.parse_sld(new_sld.read()).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                self.add_error("new_sld", str(e))

        if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
            self.add_error("valid_to", "The end date is before the start date.")

        raster, county = data.get("raster"), data.get("county")
        if raster and product and county and not self.errors:
            path = raster.temporary_file_path() if hasattr(raster, "temporary_file_path") else None
            if path is None:
                # Small files are kept in memory; GDAL needs a path.
                path = services.stage_upload(raster)
                self._staged_small = path
            try:
                info = inspect_raster(path)
                validate_product(info, product)
                check_county(GEOSGeometry(info.footprint_wkt, srid=4326), county, counties())
                self.raster_info = info
            except RasterError as e:
                self.add_error("raster", str(e))
                if getattr(self, "_staged_small", None):
                    os.remove(self._staged_small)
                    self._staged_small = None
        return data

    @transaction.atomic
    def save(self, user):
        data = self.cleaned_data
        style = data.get("existing_style")
        if style is None:
            style = PestStyle.objects.create(
                name=data["new_style_name"], title=data.get("new_style_title") or data["new_style_name"],
                product_type=data["product_type"], sld_body=data["new_sld_body"], created_by=user)
            geoserver.save_style(style)

        layer = super().save(commit=False)
        layer.style = style
        layer.temporal_type = TemporalType.BASELINE
        layer.uploaded_by = user
        layer.source_filename = data["raster"].name[:255]
        layer.file_size = data["raster"].size
        layer.staged_path = getattr(self, "_staged_small", None) or services.stage_upload(data["raster"])
        if self.raster_info:
            layer.footprint = GEOSGeometry(self.raster_info.footprint_wkt, srid=4326)
        layer.save()
        return layer


class PestLayerEditForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = PestLayer
        fields = ["pest", "county", "style", "valid_from", "valid_to", "title", "description", "is_published"]
        widgets = {"valid_from": DateInput(), "valid_to": DateInput(),
                   "description": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_published": "Show on the dashboard"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["county"].queryset = counties()
        self.fields["style"].queryset = PestStyle.objects.filter(product_type=self.instance.product_type)
        self._bootstrap()

    def clean(self):
        data = super().clean()
        if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
            self.add_error("valid_to", "The end date is before the start date.")
        county = data.get("county")
        if county and "county" in self.changed_data and self.instance.footprint:
            try:
                check_county(self.instance.footprint, county, counties())
            except RasterError as e:
                self.add_error("county", str(e))
        return data


class PestStyleForm(BootstrapMixin, forms.ModelForm):
    sld_file = forms.FileField(required=False, label="SLD file", help_text="Upload a file or paste the SLD below.")

    class Meta:
        model = PestStyle
        fields = ["name", "title", "product_type", "sld_file", "sld_body"]
        widgets = {"sld_body": forms.Textarea(attrs={"rows": 14, "class": "form-control pw-code"})}
        labels = {"sld_body": "SLD"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sld_body"].required = False
        if self.instance.pk:
            # The name is the GeoServer style name that layers point at.
            self.fields["name"].disabled = True
            if self.instance.pestlayer_set.exists():
                self.fields["product_type"].disabled = True
        self.fields["sld_file"].widget.attrs["accept"] = ".sld,.xml"
        self._bootstrap()

    def clean(self):
        data = super().clean()
        body = data.get("sld_body")
        if data.get("sld_file"):
            try:
                body = data["sld_file"].read().decode("utf-8")
            except UnicodeDecodeError:
                self.add_error("sld_file", "The SLD file must be UTF-8 text.")
                return data
        if not body:
            self.add_error("sld_body", "Provide the SLD.")
            return data
        try:
            geoserver.parse_sld(body)
        except ValueError as e:
            self.add_error("sld_file" if data.get("sld_file") else "sld_body", str(e))
        data["sld_body"] = body
        return data


def boundary_datasets():
    return Dataset.objects.filter(subtype="vector").order_by("title")


class BoundaryImportForm(BootstrapMixin, forms.ModelForm):
    dataset = forms.ModelChoiceField(
        queryset=Dataset.objects.none(),
        help_text="Upload the boundaries through GeoNode first (Data → Upload dataset), then pick them here.")

    class Meta:
        model = BoundarySource
        fields = ["dataset", "level", "name_field", "code_field", "parent_field", "limit_values"]
        widgets = {"limit_values": forms.TextInput()}
        labels = {"parent_field": "Parent name field", "limit_values": "Only keep"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dataset"].queryset = boundary_datasets()
        for name in ("name_field", "code_field", "parent_field"):
            self.fields[name].widget.attrs["list"] = "pw-dataset-fields"
        self._bootstrap()

    def clean(self):
        data = super().clean()
        dataset, level = data.get("dataset"), data.get("level")
        if not dataset or not level:
            return data
        try:
            fields = dataset_fields(dataset)
        except BoundaryImportError as e:
            self.add_error("dataset", str(e))
            return data
        for name in ("name_field", "code_field", "parent_field"):
            value = data.get(name)
            if value and value not in fields:
                self.add_error(name, f"Not an attribute of this dataset. Available: {', '.join(fields)}.")
        if level == AdminLevel.COUNTY and data.get("parent_field"):
            self.add_error("parent_field", "Counties have no parent.")
        parent_level = PARENT_LEVEL.get(level)
        if parent_level and not data.get("parent_field") and \
                not AdminBoundary.objects.filter(level=parent_level).exists():
            self.add_error(
                "parent_field",
                f"No {AdminLevel(parent_level).label.lower()} boundaries exist yet, so they can't be found "
                f"spatially. Import them first, or name the attribute that holds the "
                f"{AdminLevel(parent_level).label.lower()} name.")
        return data

    def save(self, user):
        source = super().save(commit=False)
        source.dataset_name = self.cleaned_data["dataset"].alternate
        source.imported_by = user
        source.save()
        return source


# --- model runs and input data -------------------------------------------------------

class MonthInput(forms.DateInput):
    input_type = "month"


def _model_county_choices():
    from .orchestrator import counties_with_models
    with_models = set(counties_with_models())
    return [(c.name, c.name + ("" if c.name in with_models else " (no models)")) for c in counties()]


class StartRunForm(BootstrapMixin, forms.Form):
    """Start a pipeline by hand, e.g. to backfill a period or re-run a forecast."""
    pipeline = forms.ChoiceField(choices=Pipeline.choices, widget=forms.RadioSelect)
    counties = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
                                         help_text="None ticked = every county that has pest models.")
    start = forms.DateField(required=False, widget=DateInput(), label="From",
                            help_text="Weather: first day to download. Nowcast: first map date.")
    end = forms.DateField(required=False, widget=DateInput(), label="To",
                          help_text="Weather: last day. Nowcast: last map date (one map every 7 days in between).")
    issue = forms.CharField(required=False, widget=MonthInput(), label="Forecast issue month",
                            help_text="The SEAS5 start month. Its satellite composites for the month before must be uploaded.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counties"].choices = _model_county_choices()
        self._bootstrap()

    def clean_issue(self):
        v = self.cleaned_data.get("issue") or ""
        if v and not re.match(r"^\d{4}-\d{2}$", v):
            raise forms.ValidationError("Use the form YYYY-MM.")
        return v

    def clean(self):
        data = super().clean()
        p, start, end = data.get("pipeline"), data.get("start"), data.get("end")
        if start and end and start > end:
            self.add_error("end", "The end is before the start.")
        if p == Pipeline.WEATHER and not (start and end):
            self.add_error("start", "Give the period to download.")
        if p == Pipeline.FORECAST and not data.get("issue"):
            self.add_error("issue", "Choose the issue month.")
        return data

    def params(self):
        import datetime as dt
        from .orchestrator import default_params
        d = self.cleaned_data
        p = default_params(d["pipeline"])
        if d.get("counties"):
            p["counties"] = list(d["counties"])
        if d["pipeline"] == Pipeline.WEATHER:
            p.update(start=str(d["start"]), end=str(d["end"]))
        elif d["pipeline"] == Pipeline.NOWCAST:
            if d.get("start") or d.get("end"):
                start, end = d.get("start") or d["end"], d.get("end") or d["start"]
                dates, day = [], end
                while day >= start:
                    dates.append(str(day))
                    day -= dt.timedelta(days=7)
                p["map_dates"] = sorted(dates)
        elif d["pipeline"] == Pipeline.FORECAST:
            p["issue"] = d["issue"]
        return p


class InputUploadForm(BootstrapMixin, forms.Form):
    county = forms.ModelChoiceField(queryset=AdminBoundary.objects.none())
    month = forms.CharField(required=False, widget=MonthInput(),
                            help_text="Monthly datasets: the month the composite is for. Leave blank for a .zip "
                                      "of several months (each file name must contain YYYY_MM).")
    file = forms.FileField()

    def __init__(self, dataset, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dataset = dataset
        self.fields["county"].queryset = counties()
        self.fields["file"].widget.attrs["accept"] = ",".join(dataset.ext)
        self.fields["file"].help_text = f"{' or '.join(dataset.ext)}. {dataset.notes}"
        if not dataset.monthly:
            del self.fields["month"]
        self._bootstrap()

    def clean_month(self):
        v = self.cleaned_data.get("month") or ""
        if not v:
            return None
        if not re.match(r"^\d{4}-\d{2}$", v):
            raise forms.ValidationError("Use the form YYYY-MM.")
        return int(v[:4]), int(v[5:7])

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse

from .. import conf
from ..models import TemporalType
from ..zonal import distribution_keys

# What each map page shows. Uploaded "baseline" layers appear with the nowcast.
MODES = {
    "nowcast": dict(temporal=[TemporalType.NOWCAST, TemporalType.BASELINE], title="Nowcast",
                    intro="Current risk, from observed weather and the latest satellite data. Updated every week."),
    "forecast": dict(temporal=[TemporalType.FORECAST], title="Forecast",
                     intro="Expected risk for the coming months, from the seasonal weather forecast and forecast "
                           "vegetation. Updated every month."),
}


def portal_context(request, section):
    user = request.user
    return {
        "section": section,
        "can_manage": user.has_perm("pestwatch.view_pestlayer"),
        "can_processes": user.has_perm("pestwatch.view_pipelinerun"),
        "can_inputs": user.has_perm("pestwatch.view_inputupload"),
    }


def _map_page(request, mode):
    m = MODES[mode]
    config = {
        "mode": mode,
        "wmsUrl": f"{settings.GEOSERVER_PUBLIC_LOCATION.rstrip('/')}/ows",
        "catalogUrl": reverse("pestwatch:api_catalog") + "?temporal=" + ",".join(m["temporal"]),
        "boundariesUrl": reverse("pestwatch:api_boundaries"),
        "statsUrl": reverse("pestwatch:api_stats"),
        "classes": [{"value": v, "label": label, "color": conf.get("CLASS_COLORS").get(v, "#999")}
                    for v, label in sorted(conf.get("CLASSES").items())],
        "hybridBins": distribution_keys("hybrid"),
        "emptyText": ("No forecast has been published yet. Forecasts appear here after the monthly run."
                      if mode == "forecast" else "No nowcast has been published yet for this selection."),
    }
    return render(request, "pestwatch/dashboard.html",
                  {**portal_context(request, mode), "config": config, "mode": m})


def index(request):
    return _map_page(request, "nowcast")


def forecast(request):
    return _map_page(request, "forecast")


def about(request):
    return render(request, "pestwatch/about.html", {
        **portal_context(request, "about"),
        "classes": [{"value": v, "label": label, "color": conf.get("CLASS_COLORS").get(v, "#999")}
                    for v, label in sorted(conf.get("CLASSES").items())],
        "schedule": conf.get("SCHEDULE"),
        "nowcast_day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
            int(conf.get("SCHEDULE").get("nowcast", {}).get("weekday", 0))],
        "forecast_months": conf.get("FORECAST_MONTHS"),
        "map_day": conf.get("FORECAST_MAP_DAY"),
    })

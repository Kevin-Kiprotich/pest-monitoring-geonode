from django.urls import path

from .views import api, manage, pages, processes

app_name = "pestwatch"

urlpatterns = [
    path("", pages.index, name="index"),
    path("forecast/", pages.forecast, name="forecast"),
    path("how-it-works/", pages.about, name="about"),

    path("layers/", manage.layer_list, name="layer_list"),
    path("layers/upload/", manage.layer_upload, name="layer_upload"),
    path("layers/<int:pk>/", manage.layer_detail, name="layer_detail"),
    path("layers/<int:pk>/edit/", manage.layer_edit, name="layer_edit"),
    path("layers/<int:pk>/delete/", manage.layer_delete, name="layer_delete"),
    path("layers/<int:pk>/recompute/", manage.layer_recompute, name="layer_recompute"),
    path("layers/<int:pk>/toggle-publish/", manage.layer_toggle_publish, name="layer_toggle_publish"),

    path("layers/boundaries/", manage.boundary_list, name="boundary_list"),
    path("layers/boundaries/import/", manage.boundary_import, name="boundary_import"),
    path("layers/boundaries/<int:pk>/reimport/", manage.boundary_reimport, name="boundary_reimport"),
    path("layers/boundaries/<int:pk>/delete/", manage.boundary_delete, name="boundary_delete"),

    path("layers/styles/", manage.style_list, name="style_list"),
    path("layers/styles/new/", manage.style_create, name="style_create"),
    path("layers/styles/<int:pk>/edit/", manage.style_edit, name="style_edit"),
    path("layers/styles/<int:pk>/delete/", manage.style_delete, name="style_delete"),

    path("processes/", processes.process_list, name="process_list"),
    path("processes/start/", processes.process_start, name="process_start"),
    path("processes/<int:pk>/", processes.process_detail, name="process_detail"),
    path("processes/<int:pk>/status/", processes.process_status, name="process_status"),
    path("processes/<int:pk>/retry/", processes.process_retry, name="process_retry"),
    path("processes/<int:pk>/cancel/", processes.process_cancel, name="process_cancel"),
    path("processes/schedule/<str:pipeline>/toggle/", processes.schedule_toggle, name="schedule_toggle"),

    path("input-data/", processes.input_list, name="input_list"),
    path("input-data/<str:key>/upload/", processes.input_upload, name="input_upload"),

    path("api/catalog/", api.catalog, name="api_catalog"),
    path("api/boundaries/", api.boundaries, name="api_boundaries"),
    path("api/stats/", api.stats, name="api_stats"),
    path("api/layer-status/", api.layer_status, name="api_layer_status"),
    path("api/datasets/<int:pk>/fields/", api.dataset_attributes, name="api_dataset_fields"),
]

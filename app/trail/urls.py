from django.urls import path

from trail import views

app_name = "trail"

urlpatterns = [
    path("", views.home, name="home"),
    path("ingest/", views.ingest, name="ingest"),
    # PULL control plane: the prober asks for its orders + the ladder. The app
    # never pushes anything at a host process (spec-ping-trail-control-plane).
    path("control/<slug:vantage_slug>/", views.control, name="control"),
    # Path-segment filtering, not query params (feedback_url_path_filtering).
    path("trail/<slug:slug>/", views.dashboard, name="dashboard"),
    path("trail/<slug:slug>/tick/<int:pk>/", views.tick_detail, name="tick_detail"),
    path("trail/<slug:slug>/tick/<int:pk>/collapse/", views.tick_collapse, name="tick_collapse"),
    # Operator control of the prober (HTMX, inline — no modal).
    path("trail/<slug:slug>/control/", views.control_update, name="control_update"),
    path("trail/<slug:slug>/control/status/", views.control_status, name="control_status"),
    # Operator correction of a rung's kind/label (HTMX row swap — no modal).
    path("trail/<slug:slug>/rung/<int:pk>/", views.rung_row, name="rung_row"),
    path("trail/<slug:slug>/rung/<int:pk>/edit/", views.rung_edit, name="rung_edit"),
    path("trail/<slug:slug>/rung/<int:pk>/save/", views.rung_update, name="rung_update"),
    # Live regions — polled individually so an open detail panel survives.
    path("trail/<slug:slug>/live/stats/", views.live_stats, name="live_stats"),
    path("trail/<slug:slug>/live/chart/", views.live_chart, name="live_chart"),
    path("trail/<slug:slug>/live/rollup/", views.live_rollup, name="live_rollup"),
    path("trail/<slug:slug>/live/ticks/", views.live_ticks, name="live_ticks"),
]

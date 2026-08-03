from django.contrib import admin
from django.urls import path, include

from config.health import healthz

admin.site.site_header = "PING_TRAIL administration"
admin.site.site_title = "PING_TRAIL admin"
admin.site.index_title = "PING_TRAIL"

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("manage/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("trail.urls")),
]

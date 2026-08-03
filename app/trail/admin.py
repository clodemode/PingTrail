from django.contrib import admin

from trail.models import Probe, ProberControl, Rung, Sample, Trail, Vantage


class RungInline(admin.TabularInline):
    model = Rung
    extra = 0


@admin.register(Vantage)
class VantageAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")


@admin.register(Trail)
class TrailAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "vantage", "rung_count", "sample_count", "discovered_at")
    inlines = [RungInline]


@admin.register(Rung)
class RungAdmin(admin.ModelAdmin):
    list_display = ("trail", "depth", "kind", "host", "label", "is_active", "kind_pinned", "label_pinned")
    list_filter = ("trail", "kind", "kind_pinned", "label_pinned")


@admin.register(ProberControl)
class ProberControlAdmin(admin.ModelAdmin):
    """Orders + heartbeat. The dashboard is the real surface; this is a backstop."""

    list_display = ("vantage", "trail", "enabled", "interval_seconds", "timeout_seconds", "last_seen_at", "pid")
    list_filter = ("enabled",)
    readonly_fields = ("last_seen_at", "last_sweep_at", "pid", "hostname", "last_error", "consecutive_failures")


@admin.register(Sample)
class SampleAdmin(admin.ModelAdmin):
    list_display = ("trail", "fired_at", "vantage", "prober")
    list_filter = ("trail",)


@admin.register(Probe)
class ProbeAdmin(admin.ModelAdmin):
    list_display = ("sample", "rung", "rtt_ms", "loss_pct", "ttl", "error")

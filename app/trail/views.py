"""PING_TRAIL surface + ingest + the pull control plane.

The app STORES, GRAPHS and PUBLISHES ORDERS. It never pings, and it never
starts, stops or signals the host prober — it cannot, and faking it (docker
exec, ssh, shelling out) would break the host/container split that makes the
measurement honest. Control is PULL: the prober asks, this module answers.
"""
import json

from django.conf import settings
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from trail.attribution import attribute, segment_series
from trail.models import ProberControl, Rung, RungKind, Sample, Trail, Vantage, is_in_house, kind_meta
from trail.services import (
    IngestError,
    get_or_create_control,
    ladder_payload,
    record_check_in,
    record_sweep,
)

DEFAULT_TICKS = 60

# How often the dashboard's live regions re-fetch themselves. Short enough that
# a 30s tick lands visibly; the REGIONS poll, never the page, so an operator's
# open raw-detail panel is not blown away every cycle.
LIVE_POLL_SECONDS = 10
STATUS_POLL_SECONDS = 5


def _token_ok(request):
    supplied = request.headers.get("X-Ping-Trail-Token", "")
    return bool(supplied) and supplied == settings.INGEST_TOKEN


# ---------------------------------------------------------------- ingest ----

@csrf_exempt
@require_POST
def ingest(request):
    """POST /ingest/ — record one sweep from a host-side prober.

    Token-authenticated with a shared secret (X-Ping-Trail-Token). CSRF-exempt
    because the caller is a prober process, not a browser session.
    """
    if not _token_ok(request):
        return JsonResponse({"status": "error", "detail": "bad or missing ingest token"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"status": "error", "detail": f"invalid JSON: {exc}"}, status=400)

    try:
        trail_slug = payload["trail"]
        probes = payload["probes"]
    except (KeyError, TypeError):
        return JsonResponse(
            {"status": "error", "detail": "payload needs 'trail' and 'probes'"}, status=400
        )

    if not isinstance(probes, list) or not probes:
        return JsonResponse({"status": "error", "detail": "'probes' must be a non-empty list"}, status=400)

    try:
        trail = Trail.objects.get(slug=trail_slug)
    except Trail.DoesNotExist:
        return JsonResponse(
            {"status": "error", "detail": f"unknown trail '{trail_slug}' — run discover_trail first"},
            status=404,
        )

    fired_raw = payload.get("fired_at")
    fired_at = parse_datetime(fired_raw) if fired_raw else None
    if fired_at is None:
        fired_at = timezone.now()
    elif timezone.is_naive(fired_at):
        fired_at = timezone.make_aware(fired_at)

    try:
        sample, created, skipped = record_sweep(
            trail,
            fired_at,
            probes,
            vantage=trail.vantage,
            prober=str(payload.get("prober", ""))[:64],
            note=str(payload.get("note", ""))[:200],
        )
    except IngestError as exc:
        return JsonResponse({"status": "error", "detail": str(exc)}, status=400)

    # An ingest is also proof of life: the prober that sent it is running AND
    # sweeping. Recorded on the control row so the badge never has to guess.
    control = ProberControl.objects.filter(vantage=trail.vantage).first()
    if control is not None:
        record_check_in(
            control,
            pid=_as_int(payload.get("pid")),
            hostname=str(payload.get("hostname", ""))[:128],
            error=str(payload.get("error", ""))[:300],
            swept=True,
        )

    return JsonResponse(
        {
            "status": "recorded" if created else "duplicate",
            "sample_id": sample.pk,
            "trail": trail.slug,
            "fired_at": sample.fired_at.isoformat(),
            "probes_recorded": sample.probes.count(),
            "skipped_hosts": skipped,
        },
        status=201 if created else 200,
    )


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------- control plane ----

def control(request, vantage_slug):
    """GET /control/<vantage>/ — marching orders AND the full ladder.

    This single endpoint is what removes the prober's database. v1's `ping_sweep`
    read the ladder from the ORM, so starting it on the host needed
    DJANGO_DB_PATH pointed at the container's sqlite file — which only worked
    because host and container happen to share one file on this machine, and
    silently broke the decoupling the v1 spec asserts. The prober now asks for
    the ladder over HTTP and holds no DB configuration at all.

    The GET is itself the heartbeat. It has to be: while sweeping is disabled the
    prober ingests nothing, so an ingest-only heartbeat would age out and the
    badge would report STALE for a daemon that is polling perfectly well.
    """
    if not _token_ok(request):
        return JsonResponse({"status": "error", "detail": "bad or missing token"}, status=401)

    try:
        vantage = Vantage.objects.get(slug=vantage_slug)
    except Vantage.DoesNotExist:
        return JsonResponse(
            {"status": "error", "detail": f"unknown vantage '{vantage_slug}'"}, status=404
        )

    ctl = get_or_create_control(vantage)
    record_check_in(
        ctl,
        pid=_as_int(request.headers.get("X-Ping-Trail-Pid")),
        hostname=request.headers.get("X-Ping-Trail-Host", ""),
    )

    trail = ctl.trail
    return JsonResponse(
        {
            "status": "ok",
            "vantage": vantage.slug,
            "enabled": ctl.enabled,
            "interval_seconds": ctl.interval_seconds,
            "timeout_seconds": ctl.timeout_seconds,
            "trail": trail.slug if trail else None,
            "ladder": ladder_payload(trail),
            "server_time": timezone.now().isoformat(),
        }
    )


@require_POST
def control_update(request, slug):
    """HTMX POST from the dashboard — publish new orders, re-render the card.

    Writes intent only. Nothing here touches a process: the prober picks the
    change up on its next poll, which is why the copy on the card says the
    toggle controls SWEEPING rather than the daemon.
    """
    trail = get_object_or_404(Trail.objects.select_related("vantage"), slug=slug)
    ctl = get_or_create_control(trail.vantage, trail=trail)

    action = request.POST.get("action", "")
    if action == "toggle":
        ctl.enabled = not ctl.enabled
    elif action == "settings":
        interval = _as_int(request.POST.get("interval_seconds"))
        if interval is not None:
            ctl.interval_seconds = max(5, min(3600, interval))
        try:
            timeout = float(request.POST.get("timeout_seconds"))
        except (TypeError, ValueError):
            timeout = None
        if timeout is not None:
            ctl.timeout_seconds = max(0.5, min(30.0, timeout))
    # The trail this vantage sweeps is settable here too — one vantage, one
    # ladder at a time.
    if request.POST.get("trail_slug"):
        target = Trail.objects.filter(slug=request.POST["trail_slug"], vantage=trail.vantage).first()
        if target is not None:
            ctl.trail = target
    ctl.save()

    return render(
        request,
        "trail/_control_card.html",
        {"trail": trail, "control": ctl, "status_poll": STATUS_POLL_SECONDS},
    )


def control_status(request, slug):
    """HTMX poll — just the badge. Read from the heartbeat, never guessed."""
    trail = get_object_or_404(Trail.objects.select_related("vantage"), slug=slug)
    ctl = get_or_create_control(trail.vantage, trail=trail)
    return render(
        request,
        "trail/_prober_status.html",
        {"trail": trail, "control": ctl, "status_poll": STATUS_POLL_SECONDS},
    )


# --------------------------------------------------------------- surface ----

def _trails_with_counts():
    return (
        Trail.objects.select_related("vantage")
        .annotate(n_samples=Count("samples", distinct=True), n_rungs=Count("rungs", distinct=True))
        .order_by("name")
    )


def home(request):
    trail = Trail.objects.order_by("name").first()
    if trail is None:
        return render(request, "trail/empty.html", {"trails": []})
    return redirect("trail:dashboard", slug=trail.slug)


def _limit_from(request):
    try:
        return max(1, min(500, int(request.GET.get("ticks", DEFAULT_TICKS))))
    except (TypeError, ValueError):
        return DEFAULT_TICKS


def _dashboard_context(trail, limit):
    """Everything the canvas and its live regions render from.

    Shared by the full page and by each polled region so a region can never
    drift from the page it lives in.
    """
    samples = list(
        Sample.objects.filter(trail=trail)
        .prefetch_related("probes__rung")
        .order_by("-fired_at")[:limit]
    )[::-1]

    ticks = [attribute(s) for s in samples]
    categories, series, totals, inversion_points, void_indices = segment_series(ticks)

    live = [t for t in ticks if not t.void]
    rollup = _rollup(live)

    chart = {
        "categories": categories,
        "series": series,
        # Pre-computed bucket totals — a stacked-bar label formatter only ever
        # sees ONE segment via p.value (echarts-stacked-label-total).
        "totals": totals,
        "inversions": inversion_points,
        "voids": void_indices,
        # Parallel to `categories`: lets a bar click resolve its Sample for the
        # inline HTMX detail panel.
        "sample_ids": [t.sample_id for t in ticks],
    }

    return {
        "trail": trail,
        "trails": _trails_with_counts(),
        "rungs": trail.rungs.filter(is_active=True),
        "inactive_rungs": trail.rungs.filter(is_active=False),
        "ticks": list(reversed(ticks)),  # newest first in the table
        "tick_count": len(ticks),
        "void_count": len(void_indices),
        "inversion_count": sum(len(t.inversions) for t in ticks),
        "loss_tick_count": sum(1 for t in ticks if t.has_partial_loss),
        "rollup": rollup,
        "limit": limit,
        # Passed as a dict — the template's |json_script does the encoding.
        "chart": chart,
        "latest": ticks[-1] if ticks else None,
        "live_poll": LIVE_POLL_SECONDS,
        "status_poll": STATUS_POLL_SECONDS,
        "kind_choices": RungKind.choices,
    }


def dashboard(request, slug):
    """The single dense canvas: ladder, stacked-bar attribution, tick table."""
    trail = get_object_or_404(Trail.objects.select_related("vantage"), slug=slug)
    context = _dashboard_context(trail, _limit_from(request))
    context["control"] = get_or_create_control(trail.vantage, trail=trail)
    return render(request, "trail/dashboard.html", context)


# ---- live regions -----------------------------------------------------------
# HTMX polls the REGIONS, never the page. The inline raw-detail panel lives
# OUTSIDE every polled region, so an operator reading a tick's raw RTTs keeps
# reading them while ticks continue to land around it.

def _live_region(request, slug, template):
    trail = get_object_or_404(Trail.objects.select_related("vantage"), slug=slug)
    return render(request, template, _dashboard_context(trail, _limit_from(request)))


def live_stats(request, slug):
    return _live_region(request, slug, "trail/_stat_band.html")


def live_chart(request, slug):
    return _live_region(request, slug, "trail/_chart_region.html")


def live_rollup(request, slug):
    return _live_region(request, slug, "trail/_rollup_region.html")


def live_ticks(request, slug):
    return _live_region(request, slug, "trail/_ticks_region.html")


# The ladder deliberately does NOT poll: it holds the inline edit form, and a
# poll would yank a half-typed correction out from under the operator. It
# refreshes a row at a time, when that row is saved.


# ---- operator correction of a rung -----------------------------------------
# Discovery guesses from address space; the operator knows what the box is.
# Inline row swap, no modal (spec-no-modals-htmx-inline-expansion).

def _rung_or_404(slug, pk):
    trail = get_object_or_404(Trail.objects.select_related("vantage"), slug=slug)
    rung = get_object_or_404(Rung, pk=pk, trail=trail)
    return trail, rung


def rung_row(request, slug, pk):
    """Read-only row — also the Cancel target for the edit form."""
    trail, rung = _rung_or_404(slug, pk)
    return render(request, "trail/_rung_row.html", {"trail": trail, "rung": rung})


def rung_edit(request, slug, pk):
    """Inline edit form, swapped in place of the row."""
    trail, rung = _rung_or_404(slug, pk)
    return render(
        request,
        "trail/_rung_edit.html",
        {"trail": trail, "rung": rung, "kind_choices": RungKind.choices},
    )


@require_POST
def rung_update(request, slug, pk):
    """Save the operator's correction and PIN it against re-discovery."""
    trail, rung = _rung_or_404(slug, pk)

    kind = request.POST.get("kind", "").strip()
    label = request.POST.get("label", "").strip()

    if kind and kind in RungKind.values and kind != rung.kind:
        rung.kind = kind
        rung.kind_pinned = True
    if label and label != rung.label:
        rung.label = label[:64]
        rung.label_pinned = True

    # An explicit un-pin hands the rung back to discovery.
    if request.POST.get("unpin"):
        rung.kind_pinned = False
        rung.label_pinned = False

    rung.save(update_fields=["kind", "label", "kind_pinned", "label_pinned"])
    return render(request, "trail/_rung_row.html", {"trail": trail, "rung": rung})


def tick_detail(request, slug, pk):
    """HTMX inline expansion — per-tick raw RTT detail. No modal."""
    trail = get_object_or_404(Trail, slug=slug)
    try:
        sample = (
            Sample.objects.filter(trail=trail).prefetch_related("probes__rung").get(pk=pk)
        )
    except Sample.DoesNotExist:
        raise Http404("no such tick on this trail")

    tick = attribute(sample)
    rows = []
    for level in tick.levels:
        segment = next((s for s in tick.segments if s.to_depth == level.depth), None)
        rows.append({"level": level, "segment": segment, "meta": kind_meta(level.kind)})

    return render(
        request,
        "trail/_tick_detail.html",
        {"trail": trail, "sample": sample, "tick": tick, "rows": rows},
    )


def tick_collapse(request, slug, pk):
    """Empty the inline panel (Cancel affordance for the expanded region)."""
    return render(request, "trail/_tick_collapse.html", {"slug": slug, "pk": pk})


def _rollup(live_ticks):
    """Mean contribution per segment across the window.

    Also splits the total into IN-HOUSE and ISP. The v1 defect was not a bad
    number, it was a bad noun: the headline said "ISP" about a box in the
    house. The verdict is now computed from the rung KIND, so the split cannot
    disagree with the classification.
    """
    totals, counts = {}, {}
    meta = {}
    for tick in live_ticks:
        for segment in tick.segments:
            totals[segment.label] = totals.get(segment.label, 0.0) + segment.delta_ms
            counts[segment.label] = counts.get(segment.label, 0) + 1
            meta[segment.label] = segment.kind
    rows = []
    for label, total in totals.items():
        kind = meta[label]
        rows.append(
            {
                "label": label,
                "kind": kind,
                "meta": kind_meta(kind),
                "mean_ms": total / counts[label],
                "samples": counts[label],
                "in_house": is_in_house(kind),
            }
        )
    rows.sort(key=lambda r: r["mean_ms"], reverse=True)
    grand = sum(r["mean_ms"] for r in rows)
    for row in rows:
        row["share"] = (100.0 * row["mean_ms"] / grand) if grand else 0.0
        row["bar_width"] = max(0.0, row["share"])
        row["inverted"] = row["mean_ms"] < 0

    in_house_ms = sum(r["mean_ms"] for r in rows if r["in_house"])
    isp_ms = sum(r["mean_ms"] for r in rows if not r["in_house"])
    dominant = rows[0] if rows else None

    # The worst segment on EACH side of the demarc. A dominant ISP hop does not
    # excuse a sick LAN, and v1's sample window had both at once — the spec
    # calls that out explicitly.
    worst_in_house = next((r for r in rows if r["in_house"] and r["mean_ms"] > 0), None)
    worst_isp = next((r for r in rows if not r["in_house"] and r["mean_ms"] > 0), None)

    return {
        "rows": rows,
        "grand_ms": grand,
        "dominant": dominant,
        "in_house_ms": in_house_ms,
        "isp_ms": isp_ms,
        "in_house_share": (100.0 * in_house_ms / grand) if grand else 0.0,
        "isp_share": (100.0 * isp_ms / grand) if grand else 0.0,
        "verdict": ("in-house" if dominant["in_house"] else "ISP") if dominant else None,
        "worst_in_house": worst_in_house,
        "worst_isp": worst_isp,
    }

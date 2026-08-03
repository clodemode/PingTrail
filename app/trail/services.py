"""Write paths shared by the ingest endpoint and the management commands.

One function records a sweep, so a tick written by the HTTP ingest and a tick
written locally cannot drift apart in shape.
"""
from django.db import transaction
from django.utils import timezone

from trail.models import Probe, ProberControl, Rung, Sample, Trail, Vantage  # noqa: F401


class IngestError(Exception):
    """Payload the app understood well enough to reject with a reason."""


@transaction.atomic
def record_sweep(trail, fired_at, probe_rows, vantage=None, prober="", note=""):
    """Persist one tick.

    probe_rows: iterable of dicts with at least {'host'}; optional keys
    rtt_ms / sent / received / loss_pct / ttl / error.

    Returns (sample, created, skipped_hosts). A missing rtt_ms means TIMEOUT and
    is stored as NULL — never coerced to 0.0, and never dropped, because loss is
    first-class signal.
    """
    sample, created = Sample.objects.get_or_create(
        trail=trail,
        fired_at=fired_at,
        defaults={"vantage": vantage or trail.vantage, "prober": prober, "note": note},
    )
    if not created:
        # Idempotent: a retried POST of the same tick is not an error.
        return sample, False, []

    rungs = {r.host: r for r in trail.rungs.all()}
    skipped = []
    for row in probe_rows:
        host = row.get("host")
        rung = rungs.get(host)
        if rung is None:
            # A prober reporting a host that is not on the ladder is a real
            # mismatch (stale prober, re-discovered trail). Name it; don't
            # silently invent a rung.
            skipped.append(host)
            continue
        rtt = row.get("rtt_ms")
        sent = int(row.get("sent", 1) or 1)
        received = int(row.get("received", 0) or 0)
        if "loss_pct" in row and row["loss_pct"] is not None:
            loss = float(row["loss_pct"])
        else:
            loss = 100.0 * (sent - received) / sent if sent else 100.0
        Probe.objects.create(
            sample=sample,
            rung=rung,
            rtt_ms=None if rtt is None else float(rtt),
            sent=sent,
            received=received,
            loss_pct=loss,
            ttl=row.get("ttl"),
            error=(row.get("error") or "")[:200],
        )
    return sample, True, skipped


@transaction.atomic
def apply_ladder(trail, rungs, deactivate_missing=True):
    """Idempotently reconcile a Trail's rungs against a discovered ladder.

    Re-running when the ISP path changes UPDATES the ladder rather than
    duplicating it. Rungs that fall off the path are DEACTIVATED, never deleted:
    deleting would cascade away their historical probes and rewrite the past.

    OPERATOR CORRECTIONS WIN. A rung whose `kind_pinned` / `label_pinned` is set
    keeps the operator's value: discovery guesses from address space, the
    operator knows what the box actually is, and a correction that evaporated on
    the next re-discovery would be worse than no edit affordance at all.

    Returns (created, updated, deactivated, reactivated) host lists.
    """
    existing = {r.host: r for r in trail.rungs.all()}
    seen = set()
    created, updated, reactivated = [], [], []

    for spec in rungs:
        host = spec["host"]
        seen.add(host)
        rung = existing.get(host)
        if rung is None:
            Rung.objects.create(
                trail=trail,
                depth=spec["depth"],
                kind=spec["kind"],
                host=host,
                label=spec["label"],
                note=spec.get("note", ""),
                discovered_at=timezone.now(),
            )
            created.append(host)
            continue

        changed = False
        pinned = {"kind": rung.kind_pinned, "label": rung.label_pinned}
        for field in ("depth", "kind", "label"):
            if pinned.get(field):
                continue  # operator's correction — discovery does not overrule it
            if getattr(rung, field) != spec[field]:
                setattr(rung, field, spec[field])
                changed = True
        if rung.note != spec.get("note", ""):
            rung.note = spec.get("note", "")
            changed = True
        if not rung.is_active:
            rung.is_active = True
            reactivated.append(host)
            changed = True
        if changed:
            rung.discovered_at = timezone.now()
            rung.save()
            if host not in reactivated:
                updated.append(host)

    deactivated = []
    if deactivate_missing:
        for host, rung in existing.items():
            if host not in seen and rung.is_active:
                rung.is_active = False
                rung.save(update_fields=["is_active"])
                deactivated.append(host)

    trail.discovered_at = timezone.now()
    trail.save(update_fields=["discovered_at"])
    return created, updated, deactivated, reactivated


# ------------------------------------------------------------- control -----

def get_or_create_control(vantage, trail=None):
    """The orders row for a Vantage. Ships DISABLED — never auto-starts sweeping."""
    control, _created = ProberControl.objects.get_or_create(
        vantage=vantage, defaults={"trail": trail or vantage.trails.first()}
    )
    if control.trail is None:
        control.trail = trail or vantage.trails.first()
        control.save(update_fields=["trail"])
    return control


def ladder_payload(trail):
    """The ladder, as the prober needs it — over HTTP, so the prober needs no DB.

    Exactly the fields a sweep requires. The prober does not get model objects
    and does not get a database connection; this list is its whole picture of
    what to fire at.
    """
    if trail is None:
        return []
    return [
        {
            "depth": rung.depth,
            "kind": rung.kind,
            "host": rung.host,
            "label": rung.label,
        }
        for rung in trail.rungs.filter(is_active=True).order_by("depth", "host")
    ]


def record_check_in(control, pid=None, hostname="", error="", swept=False):
    """Write the prober's heartbeat. The ONLY source of the status badge.

    Called on every control poll — not only on ingest — because a paused prober
    ingests nothing at all, and a badge that went STALE the moment sweeping was
    switched off would be reporting on the wrong thing entirely (the daemon is
    fine; only the sweeping stopped).
    """
    now = timezone.now()
    fields = ["last_seen_at"]
    control.last_seen_at = now
    if pid is not None:
        control.pid = pid
        fields.append("pid")
    if hostname:
        control.hostname = hostname[:128]
        fields.append("hostname")
    if swept:
        control.last_sweep_at = now
        fields.append("last_sweep_at")
    if error:
        control.last_error = error[:300]
        control.consecutive_failures = control.consecutive_failures + 1
        fields.extend(["last_error", "consecutive_failures"])
    elif control.last_error or control.consecutive_failures:
        control.last_error = ""
        control.consecutive_failures = 0
        fields.extend(["last_error", "consecutive_failures"])
    control.save(update_fields=fields)
    return control


def get_or_create_vantage(slug, name=None):
    vantage, _ = Vantage.objects.get_or_create(
        slug=slug, defaults={"name": name or slug.replace("-", " ").title()}
    )
    return vantage


def get_or_create_trail(slug, vantage, name=None, description=""):
    trail, _ = Trail.objects.get_or_create(
        slug=slug,
        defaults={
            "vantage": vantage,
            "name": name or slug.replace("-", " ").title(),
            "description": description,
        },
    )
    return trail

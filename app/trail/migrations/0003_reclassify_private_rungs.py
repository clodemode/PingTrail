"""Re-label the ladder now that address space, not hop position, decides a kind.

Kris's 192.168.1.1 was labelled `isp_hop1` / "last mile / ISP backhaul". It is
his DSL router, still in the house. The measurements were right; only the nouns
lied — which is worse, because the headline read "ISP" for a segment he owns.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not renumber depths, does not touch primary keys, and does not create or
delete a single Rung. Every Probe keeps pointing at the same Rung row it always
did, so the collected history stays readable and `trail_report` over the OLDEST
ticks renders exactly as before — with correct nouns. Relabelling in place is
the only shape of this change that does not rewrite the past.

Only rungs discovery had classified as ISP (`isp_hop` / `isp_dns`) are eligible.
A `gateway` rung is private too, and correctly so; leaving it alone matters.
"""
import ipaddress
from datetime import timedelta

from django.db import migrations

CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Enabling sweeping is normally an explicit operator act
# (leaning-beat-schedule-must-ship-disabled), and the model default is False.
# But a prober is demonstrably sweeping right now, and a migration that silently
# switched Kris's live collection off would be a regression dressed as a default.
# So: adopt the observed state, narrowly — a vantage whose trail took a tick
# within this window is already being swept, and stays swept.
RECENTLY_SWEPT_SECONDS = 300


def _is_private(host):
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr in CGNAT


def reclassify(apps, schema_editor):
    Rung = apps.get_model("trail", "Rung")
    Trail = apps.get_model("trail", "Trail")

    for trail in Trail.objects.all():
        rungs = list(Rung.objects.filter(trail=trail).order_by("depth", "host"))

        # 1. A private address can never be an ISP rung.
        for rung in rungs:
            if rung.kind not in ("isp_hop", "isp_dns") or not _is_private(rung.host):
                continue
            was = rung.kind
            rung.kind = "home_router"
            # A private resolver is the house router answering DNS. Naming it
            # `local_dns` keeps the ROLE visible while the kind tells the truth
            # about whose box it is.
            rung.label = "local_dns" if was == "isp_dns" else "home_router"
            rung.save(update_fields=["kind", "label"])

        # 2. Re-number the labels that numbering now misdescribes. After the
        #    reclassification Kris has exactly ONE true ISP hop, so calling it
        #    `isp_hop2` implies a sibling that does not exist.
        for kind in ("home_router", "isp_hop"):
            group = [r for r in Rung.objects.filter(trail=trail, kind=kind).order_by("depth", "host")]
            for index, rung in enumerate(group, start=1):
                # `local_dns` is a role label, not a positional one — leave it.
                if kind == "home_router" and rung.label == "local_dns":
                    continue
                wanted = kind if len(group) == 1 else f"{kind}{index}"
                if rung.label != wanted:
                    rung.label = wanted
                    rung.save(update_fields=["label"])


def seed_controls(apps, schema_editor):
    """One orders row per Vantage, adopting whatever is already happening."""
    from django.utils import timezone

    Vantage = apps.get_model("trail", "Vantage")
    Sample = apps.get_model("trail", "Sample")
    ProberControl = apps.get_model("trail", "ProberControl")

    cutoff = timezone.now() - timedelta(seconds=RECENTLY_SWEPT_SECONDS)

    for vantage in Vantage.objects.all():
        if ProberControl.objects.filter(vantage=vantage).exists():
            continue
        trail = (
            Sample.objects.filter(trail__vantage=vantage, fired_at__gte=cutoff)
            .order_by("-fired_at")
            .values_list("trail", flat=True)
            .first()
        )
        Trail = apps.get_model("trail", "Trail")
        active_trail = Trail.objects.filter(pk=trail).first() if trail else None
        ProberControl.objects.create(
            vantage=vantage,
            trail=active_trail or Trail.objects.filter(vantage=vantage).first(),
            enabled=active_trail is not None,
            interval_seconds=30,
            timeout_seconds=2.0,
        )


def noop(apps, schema_editor):
    """Reverse is a no-op on purpose.

    Un-relabelling would restore the lie, and there is no honest way to know
    which rows were 'originally' wrong once they are right.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("trail", "0002_control_plane_and_rfc1918_kinds"),
    ]

    operations = [
        migrations.RunPython(reclassify, noop),
        migrations.RunPython(seed_controls, noop),
    ]

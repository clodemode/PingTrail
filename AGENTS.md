<!-- CLODE-COLD-START-GATE v3 — GENERATED from ABOUT/CLODE/GATE/cold_start_gate.py; edit the source and run `python3 ABOUT/CLODE/GATE/cold_start_gate.py bake`, never this block; the SessionStart hook injects this same ritual at turn 0 -->
> ### ⛔ COLD START — orient before your first Edit, grep, or subagent
>
> This is a **CLODE platform thread.** Inline work and a bare `Agent(subagent_type=…)` carry **none** of the contract — no LORE guidance, no DOMINO-equipped MCP tools, no HEY_LEAN presence — and are **invisible to sibling threads**, so they collide silently. Do these two **first**:
>
> 1. **`codi_orient(session=…, scope=…, klass='beacon'|'orchestrator', cwd=…, branch=…, stale_seconds=900, app_scope='ping_trail', lair=…, tag=…)`**
>    **ONE call** — registers presence on HEY_LEAN, snapshots live siblings, and returns the scoped briefing. Compose **every** briefing dimension you know (AND semantics; a bare `lair=` ranks 20 slots against 800+ rows — composed, against dozens). In this repo that means **`app_scope='ping_trail'`** plus the `lair=` you are working in. Ruled 2026-08-11: precision over capacity, cap stays 20.
>    Read **all three sections**. A sibling on your surface → freeze a HEY_LEAN baseline before touching it. Each section carries its own `status`: a briefing error does **not** mean the beacon went unwritten.
>    *Fallback, equally contract-satisfying, if `codi_orient` is unavailable or a section errors:* `agent_register(...)` → `agent_active(stale_seconds=900)` → `ido_briefing(app_scope='ping_trail', lair=…)`.
> 2. **Dispatch the work as a CODI** — not inline, not a bare subagent. Out-of-scope finds → **`lairs_lore_add(lore_type='todo')`**, *never* a `spawn_task`/chip. Every dispatch prompt — and your own thread-end — must carry the completion line: *On completion call ``codi_ledger_record(codi_slug=…, status=…, lore_refs=<every slug you consulted or obeyed, INCLUDING rows seen only as a one-line `dont` in a briefing>)``, or pass `no_lore_consulted=True` if genuinely none.*
>
> About to call Edit/Write/Task before doing 1–2? **Stop and orient.**
<!-- /CLODE-COLD-START-GATE -->

# PING_TRAIL — Agent Rules

PING_TRAIL measures **which network segment** is injecting latency, by firing ICMP at every rung of a hop ladder simultaneously and attributing the RTT deltas between adjacent rungs to the segments between them. Django 6.x + HTMX + Bootstrap 5.3 + ECharts.

Owner character: **CLOGGER 🪰** ("Watching the Pipes").

## Context Scope

Use only knowledge from this project. Do not rely on conventions or patterns from other codebases or training data unless they align with this project's explicit rules.

## Orient first

Read, in order: [README.md](README.md) for the method, [CLAUDE.md](CLAUDE.md) for the five things that actually bite, then `LORE/` — two specs and one open todo, YAML, the design record for v1 and v2.

## The hard constraints

1. **The prober is HOST-SIDE; this app never pings.** Inside Docker the default gateway is the bridge (172.x.x.1), not the router, and on macOS the Docker "host" is a Linux VM behind its own NAT. `prober.py` / `ping_sweep` / `discover_trail` run on the host or they measure a fiction.
2. **Control is PULL.** The site cannot start or stop the daemon — it publishes orders, the prober asks for them. The toggle controls SWEEPING; a **STALE** badge means the host process is gone and only a terminal fixes it.
3. **Unprivileged `SOCK_DGRAM` ICMP needs no root on macOS.** No `sudo`, no raw sockets. `/sbin/ping` is a fallback only, and it still fires in parallel.
4. **A private address (RFC1918 / CGNAT) is NEVER an ISP hop.** Address space decides, not traceroute position. `home_router` names the DSL box honestly; `isp_hop` is the first *public* address.
5. **`ping_sweep` holds no DB config by design.** It talks HTTP only. If it ever needs `DJANGO_DB_PATH` again, that is a regression, not a fix.
6. **The two secrets fail closed.** `SECRET_KEY` and `INGEST_TOKEN` have dev defaults published in `app/config/settings.py`; with `DEBUG` off, either default raises `ImproperlyConfigured` at import. Never soften that to a warning, and add any new secret to `require_configured_secrets`.

## Conventions

- **No modals** — inline HTMX expand/collapse only.
- **Poll the live regions, not the page** — a whole-page refresh blows away an operator's open detail panel.
- **Stacked BAR, category x-axis** — never line/area, never a time axis. A fully-timed-out tick is a VOID bar, never a bridge.
- **Never clamp a negative segment delta** — inversions are real signal; surface them as a marker.
- **Loss is data**, not a missing row. `Probe.rtt_ms=NULL` means timeout and is distinct from `0.0`.
- **Path-segment filtering, not query params** — `/trail/<slug>/tick/<pk>/`.
- **No CSS or JS in templates** — static files or CDN only. Cache-bust with `?v={{ bootstamp }}`.
- **Rung identity is stable** — never reclassify or renumber in a way that orphans historical Probes.
- **No real-world addresses in fixtures** — tests use RFC1918 for private rungs and `192.88.99.0/24` (RFC 7526, returned to IANA) for public ones. Note that RFC 5737 documentation addresses do *not* work as public stand-ins: Python's `ipaddress` reports all three ranges as `is_private`.

## Docker

- Service `ping-trail`, container `about-ping-trail-1`, port **8030**
- Boot: `docker compose up -d` from this directory
- Mgmt cmds run **in the container**: `docker exec about-ping-trail-1 python manage.py <cmd>` — except the three host-side ones below
- Logs: `docker logs about-ping-trail-1 --tail 100 --follow`
- Health: `curl -s http://localhost:8030/healthz`
- Dev is `gunicorn --reload` over a bind-mounted `./app` — Python edits are live; template/static edits are too, via the bootstamp

## Host-side commands (repo root, NOT the container)

```bash
.venv/bin/python app/manage.py discover_trail --trail home
.venv/bin/python app/manage.py ping_sweep --vantage home-mac --once --force
.venv/bin/python app/manage.py ping_sweep --vantage home-mac --loop
```

## Verify deterministically, then browser

`trail_report` is the verifier and it runs **first**:

```bash
docker exec about-ping-trail-1 python manage.py trail_report --trail home --limit 10
docker exec about-ping-trail-1 python manage.py test trail     # 127 tests
```

Seed a fixture trail with a known injected delay at a known rung, run `trail_report`, and assert the attribution names that rung. Only then open a browser.

## Don't disturb a live prober

A running `ping_sweep --loop` is collecting real history. Don't kill it, don't restart the container out from under it mid-sweep, and don't `discover_trail` casually — a clean pass that took the other ECMP branch will retire the sibling ISP hop (open: `LORE/clogger/todo-ping-trail-ecmp-isp-hop-flapping-retires-rungs.yaml`).

## Documentation

| File | Purpose |
|------|---------|
| **[README.md](README.md)** | What PING_TRAIL is, method, quickstart, configuration |
| **[CLAUDE.md](CLAUDE.md)** | Project overview, data model, the five things that bite |
| **LORE/** | Specs and the open todo, YAML — the design record |

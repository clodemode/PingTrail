# CLAUDE.md

## Project Overview

**PING_TRAIL** answers one question: *"our internet feels slow — WHICH SEGMENT is injecting the latency?"*

At each tick it fires ICMP echo at every rung of a **TRAIL** (an ordered ladder of targets reaching progressively deeper into the internet) **simultaneously, in one shot**. Because every rung experiences the same network conditions in that instant, the RTT differences between adjacent rungs are attributable to the segments between them. Stack the segments and the bar's total height is the RTT to the deepest rung; the band that swells is the answer.

- **Stack**: Python 3.14, Django 6.x, SQLite (WAL), Docker Compose, Gunicorn
- **Frontend**: Bootstrap 5.3 (CDN), Font Awesome 6 (CDN), HTMX (CDN), ECharts (vendored static)
- **Philosophy**: Server-rendered, HTMX-driven, no SPA, no modals
- **Owner character**: CLOGGER 🪰 ("Watching the Pipes"). Surface work: SCOUT.

## CLODE Harness Membership (since 2026-08-01)

PING_TRAIL is a TARGET_APP of the ABOUT_CLODE engineering harness:

- **Container**: `about-ping-trail-1` on **:8030**. Standalone (`docker compose up -d` from this directory) and the fleet root (`docker compose -f ../ABOUT/docker-compose.yml up -d ping-trail`) build the **same `container_name` on the same port**, so run one or the other, never both.
- **Repo home**: `clodemode/PingTrail`, branch `trunk`, remote `git@github-krisdthompson:clodemode/PingTrail.git`. CamelCaps is the CLODE_MODE marker for a public repo (`naming-clodemode-repo-case-signals-visibility`); the local checkout **directory stays `ping_trail`** because the ABOUT compose `include:` path, the `/app/fleet/ping_trail` mount, and the `ping_trail` app_scope all key off it.
- **LORE**: homed in `ping_trail/LORE/` (app_scope `ping_trail`) — two specs, `spec-ping-trail-hop-ladder-latency-attribution` (v1) and `spec-ping-trail-control-plane-and-rfc1918-classification` (v2), plus one open todo. The YAML is canonical and readable on its own; inside the harness it also materializes into DB_INTENT (`ido_briefing(app_scope='ping_trail')`).
- **Health**: `GET /healthz` (db probe + version) — compose healthcheck uses it.
- **Fleet mount**: `../ping_trail:/app/fleet/ping_trail:rw` in `ABOUT/docker-compose.yml`. Adding the mount to the compose file is not enough — `about-intent-1` must be **recreated** (`docker compose up -d intent`), not merely restarted, or `/app/fleet/ping_trail` is missing and LORE home resolution silently falls back.
- **No dedicated MCP toolbox yet** — there is no `ping_trail_manage` tool. Use `docker exec about-ping-trail-1 python manage.py <cmd>`.

## The five things that actually bite

### 1. The prober is HOST-SIDE. This app never pings.

`trail/prober.py`, `ping_sweep`, and `discover_trail` **must run on the host**, outside Docker. This is a hard constraint, not a preference:

- Inside Docker the container's default gateway is the **bridge** (172.x.x.1), not the house router.
- On macOS the Docker "host" network is a **Linux VM behind its own NAT**.

A containerized prober would measure the bridge and report a confident fiction. The Django app stores and graphs; a host-side **Vantage** owns the ICMP and POSTs to `/ingest/`.

### 2. Control is PULL. The site cannot start or stop the daemon.

The container cannot spawn or signal a host process — that split is what makes the measurement honest — so it never tries. `ProberControl` publishes marching orders; the prober fetches them every cycle from `GET /control/<vantage>/` and obeys them, heartbeating on the way past.

The UI toggle controls **SWEEPING, not the daemon**. `enabled=False` means the daemon keeps running and keeps polling, it just stops firing ICMP. Read the badge accordingly:

| Badge | Meaning | Who fixes it |
|-------|---------|--------------|
| **ALIVE** | Daemon polling, sweeping on | — |
| **STOPPED** | Daemon polling, sweeping switched off here | The UI toggle |
| **STALE** | Nothing has checked in for 2+ intervals — the host process is **gone** | A terminal, not the website |

The control GET is itself the heartbeat, deliberately: a paused prober ingests nothing, so an ingest-only heartbeat would age out and report STALE for a daemon that is polling perfectly well.

### 3. Unprivileged ICMP needs no root on macOS.

`SOCK_DGRAM` / `IPPROTO_ICMP` datagram sockets are stdlib and need no root or `sudo`. Shelling out to `/sbin/ping` is a fallback only when the datagram socket cannot be opened — and the fallback still fires every rung in parallel. Two verified macOS quirks live in `trail/prober.py`'s header; read it before touching the socket code:

- A `SOCK_DGRAM` ICMP read returns the packet **with** its 20-byte IPv4 header attached (Linux strips it) — `_icmp_offset` detects the shape rather than assuming.
- **Every** open ICMP datagram socket in the process receives a copy of **every** echo reply, and the kernel rewrites the ICMP id. Matching a reply to a rung by "which socket woke up" is therefore wrong; the symptom is a plausible-looking table that is entirely fiction. Replies are matched by **source address**.

### 4. A private address is NEVER an ISP hop.

`10/8`, `172.16/12`, `192.168/16`, and `100.64/10` (CGNAT) are decided by **address space, not traceroute position**. The v1 defect was exactly this: 192.168.1.1 was labelled `isp_hop1` when it is the DSL router still inside the house — the measurements were right and only the labels lied, which is worse, because the headline read "ISP" for a segment Kris owns.

`RungKind.HOME_ROUTER` exists so a second router inside the house is named honestly. `isp_hop` is reserved for the first **public** address. Discovery classifies via `ipaddress.ip_address(...).is_private`; the operator can correct `kind`/`label` inline, and `kind_pinned` / `label_pinned` make the correction survive the next `discover_trail`.

### 5. `ping_sweep` holds NO database config. If it needs `DJANGO_DB_PATH`, that is a regression.

v1 read the ladder through the ORM, so starting the prober on the host required `DJANGO_DB_PATH` pointed at the container's SQLite file. That only worked because host and container happen to share one file on this machine, and it silently broke the decoupling the spec asserts. As of v2 the ladder and orders arrive over HTTP from `GET /control/<vantage>/`, results leave over HTTP to `POST /ingest/`, and nothing in `ping_sweep.py` imports a model or opens a connection.

## Data model

```
Vantage ─┬─ Trail ─┬─ Rung           the ladder: what we probe, how deep
         │         └─ Sample ─ Probe the measurements: one tick, one result per rung
         └─ ProberControl            marching orders + heartbeat (pull control plane)
```

| Model | What it is |
|-------|-----------|
| **Vantage** | Where a prober runs. A model on day one so the same Trail can later be triangulated from a second point (home Mac + a Fly instance). **Do not build the second vantage yet.** |
| **Trail** | A named ladder belonging to a Vantage |
| **Rung** | An ordered target: `depth`, `kind`, `host`, `label`, `kind_pinned`, `label_pinned` |
| **Sample** | One tick — unique per `(trail, fired_at)` |
| **Probe** | One rung's result in one sample. `rtt_ms=NULL` means **timeout**, distinct from `0.0` |
| **ProberControl** | Per-Vantage `enabled` / `interval_seconds` / `timeout_seconds` + prober-written heartbeat |

`RungKind`: `loopback`, `gateway`, `home_router`, `isp_hop`, `isp_dns`, `public_dns`, `anchor`. `IN_HOUSE_KINDS` = loopback + gateway + home_router — the headline attribution says "in-house" or "ISP" rather than leaving the operator to infer it from a label.

## Critical rules

- **Never run the prober in the container** — it would measure the bridge gateway.
- **Never let the app start, stop, or signal the host prober** — no shelling out, no `docker exec`, no ssh. Control is pull-only.
- **Never classify an RFC1918 / CGNAT address as an ISP rung**, regardless of traceroute position.
- **Stacked BAR, never line/area** — a line interpolates latency across the gap between ticks, inventing measurements that were never taken. One bar per discrete tick; a fully-timed-out tick renders as a **VOID bar** (gap), never a bridge between neighbours.
- **Never clamp a negative segment delta to zero** — negative deltas are real and diagnostic (a router punts ICMP addressed *to* its own control plane to a slow path while forwarding ICMP *through* it on the fast path). Persist every raw RTT; compute deltas at render; surface inversions as a distinct marker.
- **Never probe rungs sequentially or staggered** — parallel simultaneous fire is the premise that makes the subtraction valid.
- **Loss is first-class signal, not a missing datapoint** — a rung at 100% loss whose *downstream* neighbour still answers is the strongest diagnostic this tool produces.
- **Category x-axis in ECharts, never a time axis** — evenly-spaced bars must never imply continuity across a collection gap.
- **No modals** — inline HTMX expansion. Poll the live *regions*, not the page, so an open detail panel survives.
- **Never reclassify or renumber Rungs in a way that orphans historical Probes** — depth and identity stay stable.
- **No second component framework** — Bootstrap 5.3 + HTMX + ECharts, matching the fleet.
- **Ships DISABLED** — no autostart, no beat schedule (`leaning-beat-schedule-must-ship-disabled`).

## Docker

```bash
docker compose up -d                                              # standalone
docker compose -f ../ABOUT/docker-compose.yml up -d ping-trail    # inside the fleet — pick ONE

docker logs about-ping-trail-1 --tail 100 --follow
docker exec about-ping-trail-1 python manage.py test trail       # 127 tests
docker exec about-ping-trail-1 python manage.py <cmd>
curl -s http://localhost:8030/healthz
```

Dev runs `gunicorn --reload` with `./app:/app` bind-mounted, so Python edits go live without a restart. `PING_TRAIL_CMD` overrides the command; prod uses the image `CMD` (no reload).

## Host-side prober workflow

The host venv is `.venv/` at the repo root (Python 3.14 from Homebrew). All three commands below run on the **host**, from the repo root:

```bash
.venv/bin/python app/manage.py discover_trail --trail home          # rebuild the ladder (idempotent)
.venv/bin/python app/manage.py ping_sweep --vantage home-mac --once --force
.venv/bin/python app/manage.py ping_sweep --vantage home-mac --loop  # the daemon
```

`discover_trail` reads the real default gateway, traceroutes, and parses the resolver — all of which are meaningless inside Docker. It is idempotent: rungs match by host, depths and labels update in place, and rungs that fall off the path are **deactivated, not deleted**, so their history survives.

`trail_report` is the **deterministic verifier** — run it before any browser check:

```bash
docker exec about-ping-trail-1 python manage.py trail_report --trail home --limit 10
docker exec about-ping-trail-1 python manage.py trail_report --trail home --tick 42
```

## Auth — and the fail-closed secrets

`POST /ingest/` and `GET /control/<vantage>/` are token-authenticated with a shared secret sent as the **`X-Ping-Trail-Token`** header (`PING_TRAIL_INGEST_TOKEN`, dev default `dev-ingest-token`). There is no other authentication on either endpoint.

That dev default and `SECRET_KEY`'s (`DJANGO_SIGNING_KEY` / `DJANGO_SECRET_KEY`, dev default `dev-only-change-me`) are **published constants** — this repo is public, so knowing them is free. Both therefore **fail closed**: `config.settings.require_configured_secrets` raises `ImproperlyConfigured` at import when `DEBUG` is off and either value is still its dev default. A misconfigured deployment does not boot; it does not quietly serve forgeable sessions and an open write path into the measurement record.

`DEBUG=True` (the default, `DJANGO_DEBUG=1`) keeps both defaults working, so local dev stays zero-setup. Tests: `app/trail/tests/test_settings.py` — unit tests on the rule plus a subprocess import of `config.settings` under a controlled environment, because the property that matters is that the *module* refuses to import.

- **Never widen the check to "warn"** — a warning in a log nobody reads is how a known key reaches production.
- **Never add a third secret without adding it to `require_configured_secrets`** and to the README table.

## Greenfield Policy

Greenfield codebase. No backward compatibility.

- **Remove superseded code entirely** — never comment out, never gate behind flags
- **No compatibility shims** — if something is unused, delete it
- **Plow forward** — don't drag the past

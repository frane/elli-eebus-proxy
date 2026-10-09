# elli-eebus-proxy

An EEBUS proxy between energy managers (HEMS: Solar Manager / thermondo smart, Home Assistant, evcc, …) and an **Elli** wallbox. It works around the Elli's EEBUS firmware bugs, so power limits from the energy manager reach the wallbox again.

[Deutsch](README.de.md) · Built on [pyeebus](https://github.com/frane/pyeebus) and [elli-eebus](https://github.com/frane/elli-eebus)

> **Status: early.** Tested against a simulated Elli Charger 2 (with its bugs) and against the eebus-go reference implementations, not yet against a real energy manager. Nothing is released yet.

## Why

The current Elli Charger 2 firmware has two EEBUS bugs (LPC, power limitation):

- a limit with a duration (`timePeriod`) is silently ignored;
- a limit can only be lifted with *inactive* **and** 0 W; *inactive* with the value kept fails with "Write failed".

Energy managers that follow the specification therefore can't limit the wallbox.

## How it works

```
Energy manager ──EEBUS──► elli-eebus-proxy ──EEBUS──► Elli
(Solar Manager, HA, …)    looks like the Elli         proxy is its energy manager
```

- **Toward the energy manager** the proxy is the wallbox: same brand, model, serial number, entities, features and use cases as the real Elli (copied from it), its own SKI. Measurements and states are passed on live.
- **Toward the Elli** the proxy is the energy manager (paired in the Elli web UI as "elli-eebus-proxy").
- **Power limits:** the proxy confirms them to the energy manager and sends them to the Elli the way the firmware accepts them: without a duration (the proxy keeps the time itself) and lifted with *inactive, 0 W*. With several energy managers, the lowest limit wins.
- **Failsafe:** failsafe settings go to the Elli. If an energy manager is lost (disconnected, no heartbeat for 2 minutes), the proxy applies the failsafe limit for the failsafe duration, as LPC demands. If the proxy itself is down, the Elli applies the same failsafe.
- **Elli offline:** the proxy stops its heartbeat, so energy managers notice.
- **Sniff mode:** only log what the energy manager sends (`traffic.jsonl`), pass nothing on. Start with this.

## Setup with Docker

You need an always-on Linux machine in the same network as the Elli and the energy manager (Raspberry Pi with 64-bit OS, NAS, Home Assistant host). It must run 24/7. Docker needs **host networking** for mDNS.

1. Get the compose file and the image (Docker Hub `fbandov/elli-eebus-proxy`, also `ghcr.io/frane/elli-eebus-proxy`; amd64 and arm64):

   ```bash
   mkdir elli-eebus-proxy && cd elli-eebus-proxy
   curl -O https://raw.githubusercontent.com/frane/elli-eebus-proxy/main/docker-compose.yml
   docker compose pull
   ```

2. Find the SKIs of the Elli and the energy manager:

   ```bash
   docker compose run --rm elli-eebus-proxy discover
   ```

3. Put `ELLI_SKI` and `HEMS` into `docker-compose.yml`, keep `SNIFF: "1"`, then run `docker compose up -d` and watch with `docker compose logs -f`.
4. **Pair the Elli:** open the Elli web UI and go to **Connections → HEMS connection**. Pair **elli-eebus-proxy**. Remove the old energy manager there, e.g. *SolarManagerGateway*.
5. **Pair the energy manager:** remove the Elli in the energy manager and add the "Elli" the proxy announces. Check that its SKI matches the one from `docker compose run --rm elli-eebus-proxy ski`.
6. **Sniff:** the log shows every write of the energy manager, and `data/traffic.jsonl` holds the complete EEBUS traffic. When everything looks right, set `SNIFF: "0"` and run `docker compose up -d`.

To update, run `docker compose pull && docker compose up -d`. The pairings in `./data` stay.

**Synology (Container Manager):**

1. Create the folder `docker/elli-eebus-proxy`.
2. Put `docker-compose.yml` into it and fill in the values.
3. Go to **Project → Create** and pick that folder.

Use **Container → Log** for the log.

## Configuration

| Variable | Option | Default | Meaning |
|---|---|---|---|
| `ELLI_SKI` | `--elli-ski` | – | SKI of the Elli. Without it: sniff mode with a built-in Elli Charger 2 |
| `ELLI_HOST` | `--elli-host` | mDNS | IP of the Elli |
| `ELLI_PORT` | `--elli-port` | 4711 | SHIP port of the Elli |
| `HEMS` | `--hems` | – | SKIs of energy managers, comma separated; `SKI@host:port` if mDNS doesn't find it |
| `SNIFF` | `--sniff` | 0 | 1: only log, pass nothing on to the Elli |
| `PORT` | `--port` | 4711 | SHIP port toward energy managers |
| `UPSTREAM_PORT` | `--upstream-port` | 4712 | SHIP port toward the Elli |
| `TRAFFIC_LOG` | `--no-traffic-log` | 1 | write `traffic.jsonl` (rotated, max. 40 MB) |
| `STATE_DIR` | `--state-dir` | `/data` (Docker), `~/.config/elli-eebus-proxy` | see below |
| `DEBUG` | `-v` | 0 | debug logging |
| `EEBUS_ANNOUNCE_IP` | – | detected | IP to announce via mDNS, if the NAS/host has several networks |

The state directory holds:

- `hems/`: certificate toward energy managers
- `elli/`: certificate toward the Elli
- `profile.json`: last known description of the Elli, so the proxy can start while the Elli is offline
- `limits.json`: limits and failsafe, so they survive a restart
- `traffic.jsonl`: the EEBUS traffic

**Keep this directory.** A new certificate means pairing again.

## Without Docker

You need Python 3.11 or newer.

```bash
python3 -m venv ~/elli-eebus-proxy
~/elli-eebus-proxy/bin/pip install git+https://github.com/frane/elli-eebus-proxy.git
~/elli-eebus-proxy/bin/elli-eebus-proxy discover
~/elli-eebus-proxy/bin/elli-eebus-proxy run --elli-ski <SKI> --hems <SKI> --sniff
```

As a service, put this in `/etc/systemd/system/elli-eebus-proxy.service`:

```ini
[Unit]
Description=elli-eebus-proxy
Wants=network-online.target
After=network-online.target

[Service]
User=pi
Environment=ELLI_SKI=<SKI of the Elli>
Environment=HEMS=<SKI of the energy manager>
Environment=SNIFF=1
ExecStart=/home/pi/elli-eebus-proxy/bin/elli-eebus-proxy run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl enable --now elli-eebus-proxy` and watch with `journalctl -u elli-eebus-proxy -f`.

## Home Assistant next to it

The EEBUS connection of [ha-elli-2-modbus](https://github.com/frane/ha-elli-2-modbus) can connect to the proxy instead of the Elli. Add Home Assistant's SKI (shown when setting up the integration) to `HEMS`. Limits from Home Assistant and the energy manager are combined, and the lowest wins.

## Limitations

- **Incoming connections:** the proxy accepts them only from energy managers whose certificate it already knows (a Python `ssl` limitation). It therefore connects to them itself: via mDNS, or with `SKI@host:port` in `HEMS`.
- **Different SKI:** the energy manager sees a device that looks like the Elli but has a different SKI. Energy managers that check more than brand and model may refuse it. Sniff mode shows whether this is the case.
- **Elli Charger 2 only:** only its power limit (LPC) is handled specially. Writes for the first generation (current limits per phase, OPEV) are passed on unchanged.

## Development

```bash
pip install -e ".[test]" && pytest
```

The tests run the proxy between [elli-eebus](https://github.com/frane/elli-eebus) clients and its simulated Elli Charger 2 (with the firmware bugs). With `EEBUS_GO_HARNESS` set to pyeebus' `interop/eebus-go-harness`, they also run it between an eebus-go Energy Guard and a spine-go wallbox.

MIT License.

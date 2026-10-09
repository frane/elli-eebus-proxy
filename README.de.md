# elli-eebus-proxy

Ein EEBUS-Proxy zwischen Energiemanagern (HEMS: Solar Manager / thermondo smart, Home Assistant, evcc, …) und einer **Elli**-Wallbox. Er umgeht die EEBUS-Firmwarefehler der Elli, damit Leistungslimits des Energiemanagers wieder ankommen.

[English](README.md) · Basiert auf [pyeebus](https://github.com/frane/pyeebus) und [elli-eebus](https://github.com/frane/elli-eebus)

> **Status: früh.** Getestet gegen eine simulierte Elli Charger 2 (mit ihren Fehlern) und gegen die eebus-go-Referenzimplementierungen, noch nicht gegen einen echten Energiemanager. Noch nichts released.

## Warum

Die aktuelle Firmware der Elli Charger 2 hat zwei EEBUS-Fehler (LPC, Leistungsbegrenzung):

- Ein Limit mit Dauer (`timePeriod`) wird stillschweigend ignoriert.
- Ein Limit lässt sich nur mit *inaktiv* **und** 0 W aufheben. *Inaktiv* mit beibehaltenem Wert scheitert mit „Write failed“.

Energiemanager, die sich an die Spezifikation halten, können die Wallbox deshalb nicht begrenzen.

## So funktioniert es

```
Energiemanager ──EEBUS──► elli-eebus-proxy ──EEBUS──► Elli
(Solar Manager, HA, …)    sieht aus wie die Elli      Proxy ist ihr Energiemanager
```

- **Gegenüber dem Energiemanager** ist der Proxy die Wallbox. Marke, Modell, Seriennummer, Entitäten, Features und Use Cases übernimmt er von der echten Elli. Er hat aber eine eigene SKI. Messwerte und Zustände reicht er live weiter.
- **Gegenüber der Elli** ist der Proxy der Energiemanager. In der Elli-Weboberfläche wird er als „elli-eebus-proxy“ gekoppelt.
- **Leistungslimits** bestätigt der Proxy dem Energiemanager und schickt sie so an die Elli, wie die Firmware sie annimmt: ohne Dauer (die Zeit führt der Proxy selbst) und aufgehoben mit *inaktiv, 0 W*. Bei mehreren Energiemanagern gilt das niedrigste Limit.
- **Failsafe:** Die Failsafe-Einstellungen gehen an die Elli. Fällt ein Energiemanager weg (getrennt oder 2 Minuten ohne Heartbeat), setzt der Proxy das Failsafe-Limit für die Failsafe-Dauer, wie LPC es verlangt. Fällt der Proxy selbst aus, greift die Elli auf dasselbe Failsafe zurück.
- **Elli offline:** Der Proxy stoppt seinen Heartbeat, damit Energiemanager es merken.
- **Sniff-Modus:** Der Proxy protokolliert nur, was der Energiemanager schickt (`traffic.jsonl`), und gibt nichts weiter. Damit anfangen.

## Einrichtung mit Docker

Du brauchst einen dauerhaft laufenden Linux-Rechner im selben Netz wie Elli und Energiemanager, z. B. einen Raspberry Pi mit 64-Bit-OS, ein NAS oder den Home-Assistant-Host. Er muss 24/7 laufen. Docker braucht **Host-Networking** für mDNS.

1. Compose-Datei und Image holen (Docker Hub `fbandov/elli-eebus-proxy`, auch `ghcr.io/frane/elli-eebus-proxy`; amd64 und arm64):

   ```bash
   mkdir elli-eebus-proxy && cd elli-eebus-proxy
   curl -O https://raw.githubusercontent.com/frane/elli-eebus-proxy/main/docker-compose.yml
   docker compose pull
   ```

2. SKIs der Elli und des Energiemanagers finden:

   ```bash
   docker compose run --rm elli-eebus-proxy discover
   ```

3. `ELLI_SKI` und `HEMS` in `docker-compose.yml` eintragen und `SNIFF: "1"` lassen. Dann `docker compose up -d` starten und mit `docker compose logs -f` mitlesen.
4. **Elli koppeln:** In der Elli-Weboberfläche **Verbindungen → HEMS-Verbindung** öffnen und **elli-eebus-proxy** koppeln. Den alten Energiemanager dort entfernen, z. B. *SolarManagerGateway*.
5. **Energiemanager koppeln:** Dort die Elli entfernen und die „Elli“ hinzufügen, die der Proxy ankündigt. Prüfen, ob ihre SKI zu `docker compose run --rm elli-eebus-proxy ski` passt.
6. **Mitschneiden:** Das Log zeigt jeden Schreibbefehl des Energiemanagers, `data/traffic.jsonl` den kompletten EEBUS-Verkehr. Wenn alles passt, `SNIFF: "0"` setzen und `docker compose up -d` ausführen.

Zum Aktualisieren `docker compose pull && docker compose up -d` ausführen. Die Kopplungen in `./data` bleiben erhalten.

**Synology (Container Manager):**

1. Ordner `docker/elli-eebus-proxy` anlegen.
2. `docker-compose.yml` hineinlegen und die Werte eintragen.
3. Unter **Projekt → Erstellen** diesen Ordner wählen.

Das Log findest du unter **Container → Protokoll**.

## Konfiguration

| Variable | Option | Standard | Bedeutung |
|---|---|---|---|
| `ELLI_SKI` | `--elli-ski` | – | SKI der Elli. Ohne: Sniff-Modus mit eingebauter Elli Charger 2 |
| `ELLI_HOST` | `--elli-host` | mDNS | IP der Elli |
| `ELLI_PORT` | `--elli-port` | 4711 | SHIP-Port der Elli |
| `HEMS` | `--hems` | – | SKIs der Energiemanager, kommagetrennt; `SKI@host:port`, falls mDNS sie nicht findet |
| `SNIFF` | `--sniff` | 0 | 1: nur protokollieren, nichts an die Elli weitergeben |
| `PORT` | `--port` | 4711 | SHIP-Port Richtung Energiemanager |
| `UPSTREAM_PORT` | `--upstream-port` | 4712 | SHIP-Port Richtung Elli |
| `TRAFFIC_LOG` | `--no-traffic-log` | 1 | `traffic.jsonl` schreiben (rotiert, max. 40 MB) |
| `STATE_DIR` | `--state-dir` | `/data` (Docker), `~/.config/elli-eebus-proxy` | siehe unten |
| `DEBUG` | `-v` | 0 | Debug-Logging |

Das Zustandsverzeichnis enthält:

- `hems/`: Zertifikat Richtung Energiemanager
- `elli/`: Zertifikat Richtung Elli
- `profile.json`: zuletzt bekannte Beschreibung der Elli, damit der Proxy auch bei Elli offline startet
- `limits.json`: Limits und Failsafe, damit sie einen Neustart überstehen
- `traffic.jsonl`: der EEBUS-Verkehr

**Dieses Verzeichnis behalten.** Ein neues Zertifikat heißt: neu koppeln.

## Ohne Docker

Du brauchst Python 3.11 oder neuer.

```bash
python3 -m venv ~/elli-eebus-proxy
~/elli-eebus-proxy/bin/pip install git+https://github.com/frane/elli-eebus-proxy.git
~/elli-eebus-proxy/bin/elli-eebus-proxy discover
~/elli-eebus-proxy/bin/elli-eebus-proxy run --elli-ski <SKI> --hems <SKI> --sniff
```

Als Dienst diese Datei unter `/etc/systemd/system/elli-eebus-proxy.service` anlegen:

```ini
[Unit]
Description=elli-eebus-proxy
Wants=network-online.target
After=network-online.target

[Service]
User=pi
Environment=ELLI_SKI=<SKI der Elli>
Environment=HEMS=<SKI des Energiemanagers>
Environment=SNIFF=1
ExecStart=/home/pi/elli-eebus-proxy/bin/elli-eebus-proxy run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Dann `sudo systemctl enable --now elli-eebus-proxy` ausführen und mit `journalctl -u elli-eebus-proxy -f` mitlesen.

## Home Assistant parallel

Die EEBUS-Verbindung von [ha-elli-2-modbus](https://github.com/frane/ha-elli-2-modbus) kann sich statt mit der Elli mit dem Proxy verbinden. Dazu die SKI von Home Assistant (wird beim Einrichten der Integration angezeigt) in `HEMS` aufnehmen. Limits von Home Assistant und vom Energiemanager werden zusammengeführt, das niedrigste gilt.

## Grenzen

- **Eingehende Verbindungen:** Der Proxy nimmt sie nur von Energiemanagern an, deren Zertifikat er schon kennt (eine Einschränkung von Pythons `ssl`). Deshalb verbindet er sich selbst zu ihnen: per mDNS oder mit `SKI@host:port` in `HEMS`.
- **Andere SKI:** Der Energiemanager sieht ein Gerät, das wie die Elli aussieht, aber eine andere SKI hat. Energiemanager, die mehr als Marke und Modell prüfen, könnten es ablehnen. Der Sniff-Modus zeigt, ob das so ist.
- **Nur Elli Charger 2:** Nur ihr Leistungslimit (LPC) wird gesondert behandelt. Schreibbefehle für die erste Generation (Stromlimits je Phase, OPEV) gehen unverändert durch.

## Entwicklung

```bash
pip install -e ".[test]" && pytest
```

Die Tests lassen den Proxy zwischen [elli-eebus](https://github.com/frane/elli-eebus)-Clients und dessen simulierter Elli Charger 2 (mit den Firmwarefehlern) laufen. Mit `EEBUS_GO_HARNESS` (pyeebus' `interop/eebus-go-harness`) zusätzlich zwischen einem eebus-go Energy Guard und einer spine-go-Wallbox.

MIT-Lizenz.

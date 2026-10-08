"""elli-eebus-proxy command line. Every option can also be set as environment variable (for Docker)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from pyeebus.ship import Identity, is_ski_valid
from pyeebus.ship.mdns import ShipMdns

from .proxy import NAME, HemsPeer, Proxy

DEFAULT_STATE_DIR = Path.home() / ".config" / NAME


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _bool(text: str | None, default: bool) -> bool:
    if text is None:
        return default
    return text.strip().lower() not in ("0", "false", "no", "off")


def _peers(values: list[str]) -> list[HemsPeer]:
    peers = []
    for value in values:
        for part in re.split(r"[,\s]+", value):
            if part:
                peers.append(HemsPeer.parse(part))
    for peer in peers:
        if not is_ski_valid(peer.ski):
            raise SystemExit(f"invalid energy manager SKI: {peer.ski}")
    return peers


async def discover(seconds: float) -> int:
    mdns = ShipMdns()
    await mdns.browse()
    print(f"Looking for EEBUS devices for {seconds:g} s ...")
    await asyncio.sleep(seconds)
    await mdns.close()
    for s in mdns.services.values():
        print(f"- {s.brand} {s.model} ({s.device_type}), id {s.ship_id}, at "
              f"{', '.join(s.addresses) or s.host}:{s.port}")
        print(f"    SKI {s.ski}")
    if not mdns.services:
        print("No EEBUS devices found (same network? host networking in Docker?).")
    return 0 if mdns.services else 1


def show_skis(state_dir: Path) -> int:
    hems = Identity.load_or_create(state_dir / "hems", NAME)
    elli = Identity.load_or_create(state_dir / "elli", NAME)
    print(f"As wallbox (pair this in the energy manager):     {hems.ski}")
    print(f"As energy manager (pair this in the Elli web UI): {elli.ski}  ({NAME})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NAME, description="EEBUS proxy between energy managers and an Elli wallbox")
    parser.add_argument("--state-dir", type=Path, default=Path(_env("STATE_DIR", str(DEFAULT_STATE_DIR))),
                        help="certificates, pairing, cached wallbox description, traffic log [STATE_DIR]")
    parser.add_argument("-v", "--verbose", action="store_true", default=_bool(_env("DEBUG"), False),
                        help="debug logging [DEBUG=1]")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run the proxy (default)")
    run.add_argument("--elli-ski", default=_env("ELLI_SKI"),
                     help="SKI of the wallbox; without it: sniff mode with the built-in Elli Charger 2 [ELLI_SKI]")
    run.add_argument("--sniff", action="store_true", default=_bool(_env("SNIFF"), False),
                     help="only log what energy managers send, pass nothing on to the wallbox [SNIFF=1]")
    run.add_argument("--elli-host", default=_env("ELLI_HOST"), help="wallbox address; default: mDNS [ELLI_HOST]")
    run.add_argument("--elli-port", type=int, default=int(_env("ELLI_PORT", "4711")), help="[ELLI_PORT]")
    run.add_argument("--hems", action="append", default=[_env("HEMS")] if _env("HEMS") else [],
                     help="energy manager SKI (or SKI@host:port), repeatable or comma separated [HEMS]")
    run.add_argument("--port", type=int, default=int(_env("PORT", "4711")),
                     help="SHIP port toward energy managers [PORT]")
    run.add_argument("--upstream-port", type=int, default=int(_env("UPSTREAM_PORT", "4712")),
                     help="SHIP port toward the wallbox [UPSTREAM_PORT]")
    run.add_argument("--serial", default=_env("SERIAL", "00099999"),
                     help="serial number shown in sniff mode [SERIAL]")
    run.add_argument("--no-traffic-log", action="store_true", default=not _bool(_env("TRAFFIC_LOG"), True),
                     help="don't write traffic.jsonl [TRAFFIC_LOG=0]")

    disc = sub.add_parser("discover", help="list EEBUS devices on the network (find SKIs)")
    disc.add_argument("--timeout", type=float, default=10.0)
    sub.add_parser("ski", help="show the proxy's SKIs (for pairing)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("pyeebus").setLevel(logging.WARNING)

    if args.cmd == "discover":
        return asyncio.run(discover(args.timeout))
    if args.cmd == "ski":
        return show_skis(args.state_dir)
    if args.cmd is None:
        args = parser.parse_args([*(argv if argv is not None else sys.argv[1:]), "run"])

    if args.elli_ski and not is_ski_valid(args.elli_ski):
        raise SystemExit(f"invalid wallbox SKI: {args.elli_ski}")
    proxy = Proxy(args.state_dir, elli_ski=args.elli_ski, elli_host=args.elli_host, elli_port=args.elli_port,
                  hems=_peers(args.hems), port=args.port, upstream_port=args.upstream_port, serial=args.serial,
                  traffic_log=not args.no_traffic_log, sniff=args.sniff)
    try:
        asyncio.run(proxy.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

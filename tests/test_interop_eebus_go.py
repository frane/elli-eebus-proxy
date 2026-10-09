"""The proxy between eebus-go reference implementations (local only).

Uses pyeebus' interop harness (interop/eebus-go-harness): ``-mode lpc`` is a
spine-go controllable system (stands in for the wallbox), ``-mode eg`` an
eebus-go Energy Guard with MPC monitoring (stands in for a HEMS such as Solar
Manager). Runs only when EEBUS_GO_HARNESS points to the built harness.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest
from pyeebus.ship import Identity

from elliproxy import Proxy
from elliproxy.proxy import NAME

HARNESS = os.environ.get("EEBUS_GO_HARNESS")
pytestmark = pytest.mark.skipif(not HARNESS, reason="EEBUS_GO_HARNESS not set")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.lines: list[str] = []
        self.seen = 0
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while line := await self.proc.stdout.readline():
            self.lines.append(line.decode().strip())

    async def expect(self, prefix: str, timeout: float = 10.0) -> str:
        """The next line (after the last expected one) starting with ``prefix``."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            for i in range(self.seen, len(self.lines)):
                if self.lines[i].startswith(prefix):
                    self.seen = i + 1
                    return self.lines[i]
            await asyncio.sleep(0.02)
        raise AssertionError(f"no {prefix!r} line; got {self.lines[self.seen:]}")

    async def send(self, command: str) -> None:
        self.proc.stdin.write(command.encode() + b"\n")
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._task.cancel()


async def start_harness(mode: str, trust: str, port: int) -> tuple[Harness, str]:
    proc = await asyncio.create_subprocess_exec(
        HARNESS, "-mode", mode, "-port", str(port), "-trust", trust,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    h = Harness(proc)
    ski = (await h.expect("SKI")).split()[1]
    await h.expect("READY")
    return h, ski


async def test_eebus_go_energy_guard_controls_spine_go_wallbox_through_the_proxy(tmp_path):
    upstream = Identity.load_or_create(tmp_path / "elli", NAME)
    wallbox_port = free_port()
    wallbox, wallbox_ski = await start_harness("lpc", upstream.ski, wallbox_port)
    proxy = Proxy(tmp_path, elli_ski=wallbox_ski, elli_host="127.0.0.1", elli_port=wallbox_port,
                  port=0, upstream_port=0, mdns=False, bind_host="127.0.0.1")
    hems = None
    try:
        await proxy.start()
        await asyncio.wait_for(proxy.hems_ready.wait(), 10)
        hems_port = free_port()
        hems, hems_ski = await start_harness("eg", proxy.hems_ski, hems_port)
        proxy.trust_hems(hems_ski, host="127.0.0.1", port=hems_port)
        await hems.expect("CONNECTED")
        await hems.expect("EG_EVSE")
        await hems.expect("EG_HEARTBEAT")  # the proxy's own heartbeat

        # with a duration: the wallbox gets the limit without one, the proxy ends it
        await hems.send("limit 4200 2")
        assert await hems.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wallbox.expect("LPC_LIMIT") == "LPC_LIMIT 4200 true 0s"
        assert await wallbox.expect("LPC_LIMIT", timeout=6) == "LPC_LIMIT 0 false 0s"

        await hems.send("limit 5000")
        assert await hems.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wallbox.expect("LPC_LIMIT") == "LPC_LIMIT 5000 true 0s"
        await hems.send("release")  # inactive, value kept: lifted with 0 W
        assert await hems.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wallbox.expect("LPC_LIMIT") == "LPC_LIMIT 0 false 0s"

        await wallbox.send("power 3700")
        await hems.expect("MPC_POWER 3700")
    finally:
        if hems is not None:
            await hems.stop()
        await proxy.stop()
        await wallbox.stop()


async def test_eebus_go_cem_controls_the_wallbox_through_the_virtual_ev(tmp_path):
    """eebus-go CEM with the EV use cases (like Solar Manager) -> proxy -> spine-go LPC wallbox."""
    upstream = Identity.load_or_create(tmp_path / "elli", NAME)
    wallbox_port = free_port()
    wallbox, wallbox_ski = await start_harness("lpc", upstream.ski, wallbox_port)
    proxy = Proxy(tmp_path, elli_ski=wallbox_ski, elli_host="127.0.0.1", elli_port=wallbox_port,
                  port=0, upstream_port=0, mdns=False, bind_host="127.0.0.1")
    hems = None
    try:
        await proxy.start()
        await asyncio.wait_for(proxy.hems_ready.wait(), 10)
        hems_port = free_port()
        hems, hems_ski = await start_harness("cem", proxy.hems_ski, hems_port)
        proxy.trust_hems(hems_ski, host="127.0.0.1", port=hems_port)
        await hems.expect("CONNECTED")
        # the harness writes 10 A per phase as soon as it sees the EV's limits
        assert await hems.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wallbox.expect("LPC_LIMIT") == "LPC_LIMIT 6900 true 0s"
        await wallbox.send("power 6900")
        await hems.expect("EVCEM_CURRENT 10,10,10")
    finally:
        if hems is not None:
            await hems.stop()
        await proxy.stop()
        await wallbox.stop()

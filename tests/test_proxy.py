"""The proxy between energy managers (ElliEebus clients) and the simulated Elli Charger 2 (with its bugs)."""

from __future__ import annotations

import asyncio
import json

import pytest
from ellieebus import ElliEebus
from ellieebus.simulator import SimulatedElli
from pyeebus.ship import Identity
from pyeebus.spine import Role
from pyeebus.usecases import LoadLimit

from elliproxy import Proxy
from elliproxy.proxy import NAME

NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


async def wait_for(condition, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if result := condition():
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_proxy(state_dir, sim: SimulatedElli | None, sniff: bool = False) -> Proxy:
    if sim is not None:
        # the simulator is a pyeebus server: it needs the proxy's certificate
        upstream = Identity.load_or_create(state_dir / "elli", NAME)
        sim.service.trust(upstream.ski, upstream.cert_pem)
    return Proxy(state_dir, elli_ski=sim.ski if sim else None, elli_host="127.0.0.1",
                 elli_port=sim.service.node.port if sim else 4711, port=0, upstream_port=0,
                 mdns=False, bind_host="127.0.0.1", sniff=sniff)


async def connect_hems(proxy: Proxy, state_dir, name: str = "hems") -> ElliEebus:
    # name: pyeebus servers find trusted certificates by subject, so peers need distinct names
    await asyncio.wait_for(proxy.hems_ready.wait(), 5)
    hems = ElliEebus(proxy.hems_ski, state_dir=state_dir, host="127.0.0.1", port=proxy.hems.service.node.port,
                     local_port=0, announce=False, name=name)
    proxy.trust_hems(hems.our_ski, hems.service.identity.cert_pem)
    await hems.start()
    await hems.wait_connected(5)
    await wait_for(lambda: hems.status().power_limit is not None and hems.status().failsafe_power is not None)
    return hems


@pytest.fixture
async def sim():
    sim = SimulatedElli(Identity.create("elli"), port=0, **NODE)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def setup(sim, tmp_path):
    proxy = make_proxy(tmp_path / "proxy", sim)
    await proxy.start()
    hems = await connect_hems(proxy, tmp_path / "hems")
    yield sim, proxy, hems
    await hems.stop()
    await proxy.stop()


def evse(client: ElliEebus):
    return client._evse()


async def test_energy_manager_sees_the_wallbox(setup, tmp_path):
    sim, proxy, hems = setup
    status = await wait_for(lambda: (s := hems.status()).max_power is not None and s.heartbeat_ok and s)
    assert status.profile == "lpc"
    assert (status.brand, status.model, status.serial) == ("Elli", "EVSE", "00099999")
    assert (status.failsafe_power, status.failsafe_duration) == (22000, 7200)
    assert (status.min_power, status.max_power, status.nominal_max_power) == (4140, 11040, 11040)
    remote = hems.service.remote_devices[proxy.hems_ski]
    assert remote.address == sim.device.address and remote.device_type == "ChargingStation"
    assert {u["useCaseSupport"][0]["useCaseName"] for u in remote.use_cases()} >= {
        "limitationOfPowerConsumption", "monitoringOfPowerConsumption"}

    sim.set_demand(7000)
    await wait_for(lambda: hems.status().power == 7000)
    assert (tmp_path / "proxy" / "profile.json").exists()


async def test_limit_with_duration_reaches_the_elli_and_ends(setup):
    sim, _proxy, hems = setup
    sim.set_demand(11000)
    # a standard LPC write with timePeriod; the Elli itself would ignore it
    await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(4200, True, duration=1))
    await wait_for(lambda: sim.limit == (4200, True))
    await wait_for(lambda: hems.status().power == 4200)
    await wait_for(lambda: sim.limit == (0, False), timeout=5)
    await wait_for(lambda: hems.status().power_limit_active is False)
    await wait_for(lambda: hems.status().power == 11000)


async def test_standard_release_works(setup):
    sim, _proxy, hems = setup
    await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(5000, True))
    await wait_for(lambda: sim.limit == (5000, True))
    # inactive with the value kept: the Elli would answer "Write failed"
    await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(5000, False))
    await wait_for(lambda: sim.limit == (0, False))
    lim = hems._lpc.consumption_limit(evse(hems))
    assert (lim.value, lim.is_active) == (5000, False)  # the energy manager sees what it wrote


async def test_lowest_limit_of_several_energy_managers(setup, tmp_path):
    sim, proxy, hems = setup
    second = await connect_hems(proxy, tmp_path / "hems2", name="hems2")
    try:
        await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(7000, True))
        await wait_for(lambda: sim.limit == (7000, True))
        await second._lpc.write_consumption_limit(evse(second), LoadLimit(5000, True))
        await wait_for(lambda: sim.limit == (5000, True))
        await second._lpc.write_consumption_limit(evse(second), LoadLimit(0, False))
        await wait_for(lambda: sim.limit == (7000, True))
    finally:
        await second.stop()


async def test_failsafe_is_passed_on_and_applied_when_the_manager_is_gone(setup):
    sim, proxy, hems = setup
    await hems.set_failsafe(watts=3000, duration=7200)
    config = sim.evse.feature("DeviceConfiguration", Role.SERVER)
    await wait_for(lambda: config.data["deviceConfigurationKeyValueListData"]["deviceConfigurationKeyValueData"][0]
                   ["value"]["scaledNumber"]["number"] == 3000)
    await wait_for(lambda: proxy._failsafe() == (3000, 7200))
    await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(6000, True))
    await wait_for(lambda: sim.limit == (6000, True))
    await hems.stop()
    await wait_for(lambda: sim.limit == (3000, True))
    assert proxy.arbiter.limits[hems.our_ski].failsafe


async def test_wallbox_gone_stops_heartbeats_and_cached_profile_is_used(sim, tmp_path):
    proxy = make_proxy(tmp_path / "proxy", sim)
    await proxy.start()
    try:
        await asyncio.wait_for(proxy.hems_ready.wait(), 5)
        await wait_for(lambda: proxy.elli_online)
        heartbeat = proxy.hems.device.entity((1,))
        await wait_for(lambda: heartbeat._heartbeat_task is not None)
        await sim.stop()
        await wait_for(lambda: not proxy.elli_online)
        assert heartbeat._heartbeat_task is None
    finally:
        await proxy.stop()

    again = make_proxy(tmp_path / "proxy", sim)
    await again.start()  # wallbox offline: the proxy still announces it from profile.json
    try:
        assert again.hems is not None and again.hems.device.address == sim.device.address
        assert again.hems.device.entity((1,))._heartbeat_task is None
    finally:
        await again.stop()


async def test_sniff_mode_logs_what_the_manager_sends(tmp_path):
    proxy = make_proxy(tmp_path / "proxy", None)
    await proxy.start()
    hems = await connect_hems(proxy, tmp_path / "hems")
    try:
        assert hems.status().serial == "00099999"
        await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(4200, True, duration=900))
        lim = hems._lpc.consumption_limit(evse(hems))
        assert (lim.value, lim.is_active) == (4200, True)
        lines = [json.loads(line) for line in (tmp_path / "proxy" / "traffic.jsonl").read_text().splitlines()]
        writes = [entry for entry in lines if entry["side"] == "hems" and entry["dir"] == "in"
                  and entry["msg"]["datagram"]["header"]["cmdClassifier"] == "write"]
        assert writes and "timePeriod" in json.dumps(writes[-1])
    finally:
        await hems.stop()
        await proxy.stop()


async def test_sniff_mode_with_wallbox_shows_its_data_but_passes_nothing_on(sim, tmp_path):
    sim.set_demand(6000)
    proxy = make_proxy(tmp_path / "proxy", sim, sniff=True)
    await proxy.start()
    hems = await connect_hems(proxy, tmp_path / "hems")
    try:
        await wait_for(lambda: hems.status().power == 6000)
        await hems._lpc.write_consumption_limit(evse(hems), LoadLimit(4200, True))
        await asyncio.sleep(0.5)
        assert sim.limit == (0, False)
        lim = hems._lpc.consumption_limit(evse(hems))
        assert (lim.value, lim.is_active) == (4200, True)
    finally:
        await hems.stop()
        await proxy.stop()


async def test_virtual_ev_current_limits_become_a_power_limit(setup):
    from pyeebus.usecases import PhaseLimit

    sim, proxy, hems = setup
    ev = await wait_for(lambda: hems._ev())
    assert hems.status().vehicle_connected
    sim.set_demand(9000)
    await wait_for(lambda: hems._evcem.current_per_phase(ev) == [13.0, 13.0, 13.0])
    # OPEV, as Solar Manager sends it for "max. 10 A": 10 A x 230 V x 3 phases
    await hems._opev.write_load_control_limits(ev, [PhaseLimit(p, 10) for p in "abc"])
    await wait_for(lambda: sim.limit == (6900, True))
    await hems._opev.write_load_control_limits(ev, [PhaseLimit(p, 16, is_active=False) for p in "abc"])
    await wait_for(lambda: sim.limit == (0, False))
    assert proxy.arbiter.effective() is None


async def test_no_virtual_ev_when_disabled(sim, tmp_path):
    proxy = make_proxy(tmp_path / "proxy", sim)
    proxy.virtual_ev_enabled = False
    await proxy.start()
    hems = await connect_hems(proxy, tmp_path / "hems")
    try:
        assert hems._ev() is None
        assert proxy.hems.device.entity((1, 1)) is None
    finally:
        await hems.stop()
        await proxy.stop()

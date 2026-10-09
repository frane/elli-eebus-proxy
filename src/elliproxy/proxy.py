"""elli-eebus-proxy: energy managers talk to the proxy, the proxy talks to the Elli.

Toward the wallbox the proxy is an energy manager (an :class:`ElliEebus`
client). Toward energy managers it is the wallbox (a :class:`HemsSide` built
from the wallbox's own EEBUS description). It mirrors the wallbox's data and
passes writes on, except for the LPC power limit: that one is collected from
all energy managers, the lowest active one goes to the wallbox, sent the way
the Elli firmware accepts it (no duration, lifting with ``inactive, 0 W``).

In sniff mode nothing is passed on to the wallbox: the proxy only logs what
energy managers send. With a wallbox configured it still shows the wallbox's
real description and data, without one the built-in Elli Charger 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import logging.handlers
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ellieebus import ElliEebus
from pyeebus.ship import Identity as ShipIdentity
from pyeebus.ship import ShipMdns, ShipService, TrustStore, normalize_ski
from pyeebus.spine import (
    Change,
    CmdClassifier,
    ErrorNumber,
    Event,
    EventType,
    LocalFeature,
    Message,
    RemoteEntity,
    RemoteFeature,
    Role,
    SpineError,
    spawn,
)
from pyeebus.spine.device import FN_MANUFACTURER, FN_USE_CASE
from pyeebus.spine.model import (
    as_list,
    filter_items,
    format_datetime,
    parse_datetime,
    parse_duration,
    scaled_value,
    utcnow,
)
from pyeebus.spine.update import update_data
from pyeebus.usecases import LPC, OSCEV

from .arbiter import LimitArbiter
from .hems import HemsSide
from .profile import Identity, Profile

_LOGGER = logging.getLogger(__name__)
TRAFFIC = logging.getLogger("elliproxy.traffic")

NAME = "elli-eebus-proxy"
FN_LIMITS = "loadControlLimitListData"
FN_LIMIT_DESCRIPTIONS = "loadControlLimitDescriptionListData"
FN_CONFIG = "deviceConfigurationKeyValueListData"
FN_CONFIG_DESCRIPTIONS = "deviceConfigurationKeyValueDescriptionListData"
# client features the proxy needs toward the wallbox to read everything it offers
MIRROR_CLIENT_FEATURES = ("Bill", "DeviceClassification", "DeviceConfiguration", "DeviceDiagnosis",
                          "ElectricalConnection", "Identification", "IncentiveTable", "LoadControl",
                          "Measurement", "Setpoint", "TimeSeries")
RESEND_AFTER = 10.0  # s: don't repeat the same write while the wallbox has not reported it yet
PROFILE_SAVE_INTERVAL = 300.0
MDNS_WAIT = 5.0  # s to wait for the wallbox's mDNS announcement before announcing ourselves


@dataclass
class HemsPeer:
    """An energy manager to trust: ``SKI`` or ``SKI@host:port``."""

    ski: str
    host: str | None = None
    port: int | None = None

    @classmethod
    def parse(cls, text: str) -> HemsPeer:
        ski, _, address = text.strip().partition("@")
        host, port = None, None
        if address:
            host, _, port_text = address.rpartition(":") if ":" in address else (address, "", "")
            port = int(port_text) if port_text else None
        return cls(normalize_ski(ski), host or None, port)


class Proxy:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        elli_ski: str | None = None,
        elli_host: str | None = None,
        elli_port: int = 4711,
        hems: list[HemsPeer] | tuple[HemsPeer, ...] = (),
        port: int = 4711,
        upstream_port: int = 4712,
        serial: str = "00099999",
        traffic_log: bool = True,
        mdns: bool = True,
        bind_host: str | None = None,
        sniff: bool = False,
        virtual_ev: bool = True,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.elli_ski = normalize_ski(elli_ski) if elli_ski else None
        self.elli_host, self.elli_port = elli_host, elli_port
        self.peers = list(hems)
        self.port, self.upstream_port = port, upstream_port
        self.serial = serial
        self.mdns_enabled = mdns
        self.bind_host = bind_host
        self._sniff = sniff or self.elli_ski is None
        self.virtual_ev_enabled = virtual_ev
        self.arbiter = LimitArbiter(self.state_dir / "limits.json")
        self.hems_identity = ShipIdentity.load_or_create(self.state_dir / "hems", NAME)
        self.hems_trust = TrustStore.load(self.state_dir / "hems" / "trust.json")
        self.elli: ElliEebus | None = None
        self.hems: HemsSide | None = None
        self.profile: Profile | None = None
        self.elli_online = False
        self._azc = None
        self._mdns: ShipMdns | None = None
        self._elli_service: ShipService | None = None
        self._sync_lock = asyncio.Lock()
        self._sync_handle: asyncio.TimerHandle | None = None
        self._wake_event = asyncio.Event()
        self._deadline_handle: asyncio.TimerHandle | None = None
        self._last_write: tuple[float | None, float] | None = None  # (value, monotonic time)
        self._limit_writer: str | None = None
        self._clamp_to_min = False  # the wallbox refused a limit below its minimum power
        self._tasks: list[asyncio.Task] = []
        self._profile_dirty = False
        self._traffic_handler: logging.Handler | None = None
        self.hems_ready = asyncio.Event()
        """Set once energy managers can connect (the wallbox side is announced)."""
        if traffic_log:
            self._setup_traffic_log()

    @property
    def sniff(self) -> bool:
        """Only log what energy managers send, pass nothing on to the wallbox."""
        return self._sniff

    @property
    def hems_ski(self) -> str:
        """Our SKI as wallbox (pair this in the energy manager)."""
        return self.hems_identity.ski

    # --- lifecycle --------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.mdns_enabled:
            from zeroconf.asyncio import AsyncZeroconf

            self._azc = AsyncZeroconf()
            self._mdns = ShipMdns(self._azc)
            self._mdns.add_listener(self._on_service)
            await self._mdns.browse()

        if self.elli_ski:
            self.elli = ElliEebus(
                self.elli_ski, state_dir=self.state_dir / "elli", host=self.elli_host, port=self.elli_port,
                local_port=self.upstream_port, name=NAME, zeroconf=self._azc, announce=self.mdns_enabled)
            cem = self.elli.service.entities[0]
            for feature_type in MIRROR_CLIENT_FEATURES:
                cem.add_feature(feature_type, Role.CLIENT)
            # Announce the EV charging services energy managers like Solar Manager offer: without
            # them the Elli reports "service for self-consumption charging / cost-optimized
            # charging not available" (0x401029, 0x40102A).
            OSCEV(cem).setup()
            cem.add_use_case("CEM", "coordinatedEvCharging", "1.0.1", [1, 2, 3, 4, 5, 6, 7, 8])
            self.elli.service.device.subscribe_events(self._on_elli_event)
            self.elli.service.trace = self._trace("elli")
            self.elli.add_listener(self._on_elli_status)
            await self.elli.start()
            _LOGGER.info("as energy manager toward the wallbox: %s, SKI %s (pair it in the Elli web UI)",
                         NAME, self.elli.our_ski)

        cached = Profile.load(self._profile_path)
        if self.elli_ski is None:
            profile = cached or Profile.builtin(self.serial)
            await self._start_hems(profile)
        elif cached is not None:
            await self._start_hems(cached)
        else:
            _LOGGER.info("waiting for the wallbox to describe itself before announcing it to energy managers")
        self._tasks.append(asyncio.create_task(self._apply_loop()))
        self._tasks.append(asyncio.create_task(self._save_loop()))
        self._wake()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for handle in (self._sync_handle, self._deadline_handle):
            if handle is not None:
                handle.cancel()
        if self.elli is not None and self.elli_online:
            self._save_profile()
        if self.hems is not None:
            await self.hems.stop()
        if self.elli is not None:
            await self.elli.stop()
        if self._mdns is not None:
            await self._mdns.close()
        if self._azc is not None:
            await self._azc.async_close()
        if self._traffic_handler is not None:
            TRAFFIC.removeHandler(self._traffic_handler)
            self._traffic_handler.close()

    async def run_forever(self) -> None:
        """Run until SIGINT/SIGTERM (docker stop), then shut down cleanly."""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        await self.start()
        try:
            await stop.wait()
            _LOGGER.info("stopping")
        finally:
            await self.stop()

    def trust_hems(self, ski: str, cert_pem: bytes | None = None, host: str | None = None,
                   port: int | None = None) -> None:
        """Trust an energy manager (with its certificate it can also connect to us, with
        ``host`` the proxy dials it)."""
        peer = HemsPeer(normalize_ski(ski), host, port)
        self.peers.append(peer)
        if self.hems is not None:
            self.hems.add_peer(peer.ski, host, port, cert_pem=cert_pem)
        elif cert_pem is not None:
            self.hems_trust.trust(peer.ski, cert_pem)

    @property
    def _profile_path(self) -> Path:
        return self.state_dir / "profile.json"

    async def _start_hems(self, profile: Profile) -> None:
        if self._mdns is not None and self.elli_ski:
            # announce exactly like the wallbox: wait a moment for its mDNS record
            loop = asyncio.get_running_loop()
            end = loop.time() + MDNS_WAIT
            while self._elli_service is None and loop.time() < end:
                await asyncio.sleep(0.2)
        if self._elli_service is not None:
            profile.identity = self._mdns_identity(profile)
        self.hems = HemsSide(
            profile, identity=self.hems_identity, trust=self.hems_trust, port=self.port,
            on_write=self._on_hems_write, on_hems_event=self._on_hems_event, zeroconf=self._azc,
            announce=self.mdns_enabled, discover=self.mdns_enabled, host=self.bind_host)
        self.hems.service.trace = self._trace("hems")
        self.profile = profile
        self._update_virtual_ev(profile)
        if self.arbiter.limits:
            self._own_lpc_limit()
        for peer in self.peers:
            self.hems.add_peer(peer.ski, peer.host, peer.port)
        await self.hems.start()
        self.hems.set_heartbeat(self.elli is None or self.elli_online)
        self.hems_ready.set()
        idn = profile.identity
        _LOGGER.info("as wallbox toward energy managers: %s %s (serial %s), SKI %s, port %s%s",
                     idn.brand, idn.model, idn.serial, self.hems.ski, self.hems.service.node.port,
                     " - sniff mode, nothing is passed on" if self.sniff else "")
        if not self.peers:
            _LOGGER.warning("no energy manager configured (--hems SKI): nobody can connect")

    # --- mDNS ---------------------------------------------------------------------------------------

    def _on_service(self, service: ShipService, added: bool) -> None:
        if not added or service.ski in (self.hems_ski, self.elli.our_ski if self.elli else None):
            return
        _LOGGER.info("found EEBUS device: %s %s (%s), id %s, SKI %s at %s:%s", service.brand, service.model,
                     service.device_type, service.ship_id, service.ski,
                     (service.addresses or [service.host])[0], service.port)
        if service.ski == self.elli_ski:
            self._elli_service = service

    def _mdns_identity(self, profile: Profile) -> Identity:
        guess = profile.guess_identity()
        s = self._elli_service
        if s is None:
            return profile.identity
        return Identity(brand=s.brand or guess.brand, model=s.model or guess.model,
                        serial=s.serial or guess.serial, vendor=guess.vendor,
                        ship_id=s.ship_id or guess.ship_id, device_type=s.device_type or profile.device_type)

    # --- wallbox side ---------------------------------------------------------------------------------

    def _on_elli_event(self, event: Event) -> None:
        if self.elli is None or event.ski != self.elli.remote_ski:
            return
        if event.type == EventType.DEVICE:
            if event.change == Change.ADD:
                self.elli_online = True
                _LOGGER.info("wallbox connected (%s)", event.device.address if event.device else "?")
                self._schedule_sync()
                self._wake()
            else:
                self.elli_online = False
                self._last_write = None
                _LOGGER.warning("wallbox disconnected")
                if self.hems is not None:
                    self.hems.set_heartbeat(False)
        elif event.type == EventType.ENTITY:
            if event.change == Change.ADD and event.entity is not None:
                self._read_entity(event.entity)
            self._schedule_sync()
        elif event.type == EventType.DATA and event.classifier in (CmdClassifier.REPLY, CmdClassifier.NOTIFY):
            self._profile_dirty = True
            if event.function == FN_USE_CASE:
                self._schedule_sync()
            elif event.feature is not None and self.hems is not None:
                entity = event.feature.entity.entity
                if entity == (0,):
                    if event.function == FN_MANUFACTURER:
                        self.hems.mirror_manufacturer(event.data)
                else:
                    self.hems.mirror(entity, event.feature.id, event.function, event.data)

    def _read_entity(self, entity: RemoteEntity) -> None:
        """Subscribe to and read everything the wallbox offers on this entity."""
        cem = self.elli.service.entities[0]
        for remote in entity.features:
            if remote.role != Role.SERVER:
                continue
            local = cem.feature(remote.type, Role.CLIENT)
            if local is None:
                _LOGGER.debug("no client for %s, not mirrored", remote)
                continue
            if not local.has_subscription(remote):
                spawn(_quietly(local.subscribe(remote)))
            for function, ops in remote.operations.items():
                if ops.read:
                    local.read(remote, function)

    def _schedule_sync(self) -> None:
        if self._sync_handle is None:
            loop = asyncio.get_running_loop()
            self._sync_handle = loop.call_later(0.3, lambda: spawn(self._sync()))

    async def _sync(self) -> None:
        """Bring the energy managers' view in line with the wallbox's description."""
        self._sync_handle = None
        async with self._sync_lock:
            remote = self.elli._device() if self.elli else None
            if remote is None or not remote.address:
                return
            profile = Profile.from_remote(remote)
            if self._elli_service is not None:
                profile.identity = self._mdns_identity(profile)
            if self.hems is None:
                await self._start_hems(profile)
                self._save_profile(profile)
                return
            old = self.hems.profile
            if old is not None and (old.structure(), old.use_cases) != (profile.structure(), profile.use_cases):
                _LOGGER.info("wallbox description changed, updating energy managers")
                self._update_virtual_ev(profile)
                self.hems.apply_structure(profile)
            if old is not None and (old.identity != profile.identity or old.device_address != profile.device_address):
                _LOGGER.warning("wallbox identity differs from the cached one (%s): restart the proxy to apply it",
                                profile.identity)
            self.profile = profile
            self.hems.set_heartbeat(self.elli_online)
            self._save_profile(profile)

    def _save_profile(self, profile: Profile | None = None) -> None:
        remote = self.elli._device() if self.elli else None
        if profile is None:
            if remote is None or not remote.address:
                return
            profile = Profile.from_remote(remote)
            if self._elli_service is not None:
                profile.identity = self._mdns_identity(profile)
            elif self.profile is not None:
                profile.identity = self.profile.identity
        try:
            profile.save(self._profile_path)
            self._profile_dirty = False
        except OSError as err:
            _LOGGER.warning("could not save %s: %s", self._profile_path, err)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(PROFILE_SAVE_INTERVAL)
            if self._profile_dirty and self.elli_online:
                self._save_profile()

    def _elli_feature(self, local: LocalFeature) -> RemoteFeature | None:
        remote = self.elli._device() if self.elli else None
        if remote is None:
            return None
        entity = remote.entity(local.entity.entity)
        return entity.feature_by_id(local.id) if entity else None

    # --- energy manager side ------------------------------------------------------------------------

    def _on_hems_write(self, feature: LocalFeature, msg: Message) -> SpineError | None:
        ski = msg.device.ski
        label = self.hems.label(ski) if self.hems else ski[:8]
        _LOGGER.info("energy manager %s writes %s to %s: %s", label, msg.function, feature,
                     json.dumps(msg.data, separators=(",", ":")))
        ev = self.hems.virtual_ev if self.hems else None
        if ev is not None and ev.is_mine(feature):
            return self._handle_ev_write(feature, msg)
        if self.sniff:
            if msg.function == FN_LIMITS and self._lpc_limit_id(feature) is not None:
                self._own_lpc_limit()  # keep showing what was written
            return None
        if msg.function == FN_LIMITS and (limit_id := self._lpc_limit_id(feature)) is not None:
            return self._handle_lpc_write(feature, msg, limit_id)
        if not self.elli_online:
            return SpineError(ErrorNumber.GENERAL_ERROR, "wallbox not connected")
        remote = self._elli_feature(feature)
        if remote is None:
            return SpineError(ErrorNumber.DESTINATION_UNREACHABLE, "wallbox feature not found")
        spawn(self._forward(remote, msg.function, msg.data, msg.filters, label))
        return None

    async def _forward(self, remote: RemoteFeature, function: str, data: Any, filters: list[dict], label: str) -> None:
        local = self.elli.service.entities[0].feature(remote.type, Role.CLIENT)
        try:
            if not local.has_binding(remote):
                await local.bind(remote)
            await local.write_and_wait(remote, function, data, filters)
            _LOGGER.info("passed %s from %s on to the wallbox", function, label)
        except (SpineError, TimeoutError) as err:
            _LOGGER.warning("wallbox did not accept %s from %s: %r", function, label, err)
            local.read(remote, function)  # the reply restores the energy managers' view

    @staticmethod
    def _lpc_limit_id(feature: LocalFeature) -> int | None:
        if feature.type != "LoadControl" or feature.entity.type != "EVSE":
            return None
        descs = filter_items(as_list((feature.data.get(FN_LIMIT_DESCRIPTIONS) or {})
                                     .get("loadControlLimitDescriptionData")), LPC.LIMIT_FILTER)
        return descs[0].get("limitId") if len(descs) == 1 else None

    def _own_lpc_limit(self) -> None:
        if self.hems is None:
            return
        for entity in self.hems.device.entities:
            for feature in entity.features:
                if feature.role == Role.SERVER and self._lpc_limit_id(feature) is not None:
                    self.hems.owned.add((entity.entity, feature.id, FN_LIMITS))

    def _lpc_feature(self) -> tuple[LocalFeature, int] | None:
        if self.hems is None:
            return None
        for entity in self.hems.device.entities:
            for feature in entity.features:
                if feature.role == Role.SERVER and (limit_id := self._lpc_limit_id(feature)) is not None:
                    return feature, limit_id
        return None

    def _handle_lpc_write(self, feature: LocalFeature, msg: Message, limit_id: int) -> SpineError | None:
        merged = update_data(FN_LIMITS, feature.get(FN_LIMITS), msg.data, msg.filters) or {}
        item = next((i for i in as_list(merged.get("loadControlLimitData")) if i.get("limitId") == limit_id), None)
        if item is None:
            return None
        value = scaled_value(item.get("value")) or 0.0
        active = bool(item.get("isLimitActive"))
        duration = _duration((item.get("timePeriod") or {}).get("endTime"))
        ski = msg.device.ski
        self.arbiter.set(ski, value, active, duration)
        self._limit_writer = ski
        self._own_lpc_limit()
        _LOGGER.info("limit of %s: %s", self.hems.label(ski),
                     f"{value:.0f} W" + (f" for {duration:.0f} s" if duration else "") if active else "none")
        self._wake()
        return None

    def _handle_ev_write(self, feature: LocalFeature, msg: Message) -> SpineError | None:
        """Current limits on the virtual EV (OPEV, OSCEV) become a power limit for the wallbox."""
        if msg.function != FN_LIMITS or self.sniff:
            return None
        merged = update_data(FN_LIMITS, feature.get(FN_LIMITS), msg.data, msg.filters) or {}
        watts = self.hems.virtual_ev.power_limit(merged)
        ski = msg.device.ski
        self.arbiter.set(f"{ski}:ev", watts or 0.0, watts is not None)
        _LOGGER.info("EV current limits of %s: %s", self.hems.label(ski),
                     f"{watts:.0f} W" if watts is not None else "none")
        self._wake()
        return None

    def _update_virtual_ev(self, profile: Profile) -> None:
        """Show an EV entity if the wallbox has none (Elli Charger 2), remove it if it has one."""
        if self.hems is None:
            return
        has_ev = any(e["type"] == "EV" for e in profile.entities)
        evse = next((e for e in profile.entities if e["type"] == "EVSE"), None)
        want = self.virtual_ev_enabled and not has_ev and evse is not None
        if want and self.hems.virtual_ev is None:
            low, high, phases = _current_range(evse)
            self.hems.add_virtual_ev(evse_address=tuple(evse["address"]), min_current=low, max_current=high,
                                     phases=phases)
            _LOGGER.info("showing energy managers an EV (%s phases, %g-%g A): the wallbox has none", phases, low, high)
        elif not want and self.hems.virtual_ev is not None:
            self.hems.remove_virtual_ev()

    def _on_elli_status(self, status) -> None:
        if self.hems is not None and self.hems.virtual_ev is not None:
            self.hems.virtual_ev.update_power(status.power)
        self._wake()

    def _failsafe(self) -> tuple[float | None, float | None]:
        if self.hems is None:
            return None, None
        watts = seconds = None
        for entity in self.hems.device.entities:
            feature = entity.feature("DeviceConfiguration", Role.SERVER)
            if feature is None:
                continue
            descs = as_list((feature.data.get(FN_CONFIG_DESCRIPTIONS) or {}).get(
                "deviceConfigurationKeyValueDescriptionData"))
            names = {d.get("keyId"): d.get("keyName") for d in descs}
            for item in as_list((feature.data.get(FN_CONFIG) or {}).get("deviceConfigurationKeyValueData")):
                value = item.get("value") or {}
                name = names.get(item.get("keyId"))
                if name == "failsafeConsumptionActivePowerLimit":
                    watts = scaled_value(value.get("scaledNumber"))
                elif name == "failsafeDurationMinimum" and value.get("duration"):
                    with contextlib.suppress(ValueError):
                        seconds = parse_duration(value["duration"])
        return watts, seconds

    def _on_hems_event(self, ski: str, what: str) -> None:
        if self.sniff or what not in ("disconnected", "heartbeat_lost"):
            return
        watts, seconds = self._failsafe()
        for key in (ski, f"{ski}:ev"):
            if self.arbiter.lost(key, watts, seconds):
                _LOGGER.warning("energy manager %s lost: failsafe limit %s W for %s s", ski[:8], watts, seconds)
                self._wake()

    # --- applying the limit to the wallbox ------------------------------------------------------------

    def _wake(self) -> None:
        self._wake_event.set()

    def _schedule_deadline(self) -> None:
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
            self._deadline_handle = None
        deadline = self.arbiter.next_deadline()
        if deadline is not None:
            loop = asyncio.get_running_loop()
            self._deadline_handle = loop.call_later(max(deadline - time.time(), 0) + 0.05, self._wake)

    def _expire(self) -> None:
        ended = self.arbiter.expire()
        found = self._lpc_feature()
        for ski in ended:
            _LOGGER.info("limit of %s ended", self.hems.label(ski) if self.hems else ski[:8])
            if found is not None and ski == self._limit_writer:
                feature, limit_id = found
                data = copy.deepcopy(feature.get(FN_LIMITS)) or {}
                for item in as_list(data.get("loadControlLimitData")):
                    if item.get("limitId") == limit_id:
                        item["isLimitActive"] = False
                        item.pop("timePeriod", None)
                feature.set_data(FN_LIMITS, data)

    async def _apply_loop(self) -> None:
        while True:
            await self._wake_event.wait()
            self._wake_event.clear()
            if self.sniff:
                continue
            self._expire()
            self._schedule_deadline()
            if not self.elli_online or self.elli is None or self.elli.profile != "lpc":
                continue
            try:
                await self._apply(self.arbiter.effective())
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retry later
                _LOGGER.warning("setting the wallbox limit failed: %r, retrying", err)
                self._last_write = None
                await asyncio.sleep(5)
                self._wake()

    async def _apply(self, desired: float | None) -> None:
        status = self.elli.status()
        if status.power_limit is None:
            return  # limits not read yet; the status update wakes us again
        if desired is not None and status.max_power and desired >= status.max_power:
            desired = None  # at or above the maximum: no limit
        if desired is not None and self._clamp_to_min and status.min_power and desired < status.min_power:
            desired = status.min_power  # the Elli can't pause over EEBUS: charge as little as it can
        if desired is None:
            done = not status.power_limit_active
        else:
            done = bool(status.power_limit_active) and abs((status.power_limit or 0) - desired) < 1
        if done:
            self._last_write = None
            return
        if self._last_write is not None and self._last_write[0] == desired \
                and time.monotonic() - self._last_write[1] < RESEND_AFTER:
            return
        self._last_write = (desired, time.monotonic())
        if desired is None:
            _LOGGER.info("lifting the wallbox limit")
            await self.elli.clear_power_limit()
        else:
            _LOGGER.info("setting the wallbox limit to %.0f W", desired)
            try:
                await self.elli.set_power_limit(desired)
            except SpineError:
                if self._clamp_to_min or not status.min_power or desired >= status.min_power:
                    raise
                # Elli Charger 2: "Write failed" for limits below its minimum charging power (0 W
                # included), so it can't be paused over EEBUS. Use the minimum from now on.
                self._clamp_to_min = True
                _LOGGER.warning("the wallbox refuses limits below its minimum of %.0f W (it can't be paused "
                                "over EEBUS): using %.0f W instead", status.min_power, status.min_power)
                self._last_write = (status.min_power, time.monotonic())
                await self.elli.set_power_limit(status.min_power)

    # --- traffic log ------------------------------------------------------------------------------------

    def _setup_traffic_log(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._traffic_handler = logging.handlers.RotatingFileHandler(
            self.state_dir / "traffic.jsonl", maxBytes=10_000_000, backupCount=3)
        self._traffic_handler.setFormatter(logging.Formatter("%(message)s"))
        TRAFFIC.addHandler(self._traffic_handler)
        TRAFFIC.setLevel(logging.INFO)
        TRAFFIC.propagate = False

    def _trace(self, side: str):
        def trace(ski: str, direction: str, payload: dict[str, Any]) -> None:
            if self._traffic_handler is not None:
                TRAFFIC.info(json.dumps({"t": format_datetime(), "side": side, "ski": ski, "dir": direction,
                                         "msg": payload}, separators=(",", ":")))
        return trace


def _current_range(evse: dict[str, Any]) -> tuple[float, float, int]:
    """(min A, max A, phases) from the EVSE's electrical connection data, Elli Charger 2 defaults."""
    low, high, phases = 6.0, 16.0, 3
    for f in evse["features"]:
        if f["type"] != "ElectricalConnection" or f["role"] != Role.SERVER:
            continue
        data = f.get("data") or {}
        for item in as_list((data.get("electricalConnectionDescriptionListData") or {}).get(
                "electricalConnectionDescriptionData")):
            if item.get("acConnectedPhases"):
                phases = int(item["acConnectedPhases"])
        for item in as_list((data.get("electricalConnectionPermittedValueSetListData") or {}).get(
                "electricalConnectionPermittedValueSetData")):
            for value_set in as_list(item.get("permittedValueSet")):
                for rng in as_list(value_set.get("range")):
                    lo, hi = scaled_value(rng.get("min")), scaled_value(rng.get("max"))
                    if lo is not None and hi is not None and hi <= 80:  # amps, not watts
                        return lo, hi, phases
    return low, high, phases


def _duration(end_time: str | None) -> float | None:
    """LPC timePeriod endTime: a duration (relative) or a point in time."""
    if not end_time:
        return None
    try:
        if end_time.lstrip("-").startswith("P"):
            return max(parse_duration(end_time), 0.0)
        return max((parse_datetime(end_time) - utcnow()).total_seconds(), 0.0)
    except ValueError:
        _LOGGER.warning("cannot read timePeriod endTime %r, ignoring it", end_time)
        return None


async def _quietly(coro) -> None:
    with contextlib.suppress(SpineError, TimeoutError):
        await coro

"""The wallbox the energy managers see: an EEBUS device built from a :class:`Profile`.

It announces itself like the Elli, keeps its entities, features and use cases
in sync with the profile, mirrors the wallbox's data and hands writes from
energy managers to the proxy. It also watches the energy managers: connection
and heartbeat (LPC needs both to decide on the failsafe).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyeebus.service import EebusService
from pyeebus.ship import Identity as ShipIdentity
from pyeebus.ship import RemoteAbortError, TrustStore, normalize_ski
from pyeebus.spine import Change, Event, EventType, LocalEntity, LocalFeature, Message, Role, SpineError, spawn
from pyeebus.spine.device import FN_HEARTBEAT, FN_MANUFACTURER, FN_USE_CASE

from .ev import VirtualEV
from .profile import Profile

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_LOSS = 120.0  # s without heartbeat until an energy manager counts as lost (LPC)

WriteHandler = Callable[[LocalFeature, Message], SpineError | None]
HemsEventHandler = Callable[[str, str], None]
"""Called with (ski, what): connected, disconnected, heartbeat_lost, heartbeat_resumed."""


@dataclass
class EnergyManager:
    ski: str
    name: str = ""
    last_heartbeat: float | None = None
    heartbeat_lost: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} ({self.ski[:8]})" if self.name else self.ski[:8]


class HemsSide:
    def __init__(self, profile: Profile, *, identity: ShipIdentity, trust: TrustStore, port: int,
                 on_write: WriteHandler | None = None, on_hems_event: HemsEventHandler | None = None,
                 **node_options: Any) -> None:
        idn = profile.identity
        self.service = EebusService(
            identity, brand=idn.brand, model=idn.model, serial=idn.serial, vendor=idn.vendor,
            device_type=idn.device_type or profile.device_type, entity_types=(),
            ship_id=idn.ship_id or None, port=port, trust=trust,
            heartbeat_timeout=profile.heartbeat_timeout, **node_options)
        device = self.service.device
        if profile.device_address:
            device.address = profile.device_address
        device.feature_set = profile.feature_set
        if profile.manufacturer:
            device.entity((0,)).feature("DeviceClassification", Role.SERVER).data[FN_MANUFACTURER] = \
                copy.deepcopy(profile.manufacturer)
        self.on_write = on_write
        self.on_hems_event = on_hems_event
        self.owned: set[tuple[tuple[int, ...], int, str]] = set()
        """(entity, feature, function) the proxy keeps itself instead of mirroring the wallbox."""
        self.managers: dict[str, EnergyManager] = {}
        self.profile: Profile | None = None
        self.virtual_ev: VirtualEV | None = None
        """An EV entity the wallbox doesn't have, for energy managers that need one (see ev.py)."""
        self._heartbeat_on = True
        self._started = False
        self._tasks: list[asyncio.Task] = []
        self.apply_structure(profile, announce=False)
        device.subscribe_events(self._on_event)

    @property
    def device(self):
        return self.service.device

    @property
    def ski(self) -> str:
        return self.service.ski

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        await self.service.start()
        self._started = True
        self.set_heartbeat(self._heartbeat_on)
        self._tasks.append(asyncio.create_task(self._watchdog()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.service.stop()

    def add_peer(self, ski: str, host: str | None = None, port: int | None = None,
                 cert_pem: bytes | None = None) -> None:
        """Trust an energy manager. With ``host`` it is dialled directly, else found via mDNS."""
        ski = normalize_ski(ski)
        self.service.trust(ski, cert_pem)
        if host:
            self._tasks.append(asyncio.create_task(self._dial_loop(ski, host, port or 4711)))

    async def _dial_loop(self, ski: str, host: str, port: int) -> None:
        delay = 5.0
        node = self.service.node
        while True:
            if ski in node.connections:
                await node.connections[ski].wait_closed()
                delay = 5.0
            try:
                conn = await node.connect(host, port, ski)
                delay = 5.0
                await conn.wait_closed()
            except RemoteAbortError:
                _LOGGER.info("energy manager %s rejected us: pair this wallbox (SKI %s) there", ski[:8], self.ski)
                delay = 15.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - network, TLS, SHIP: retry
                _LOGGER.debug("connecting to energy manager %s failed: %r", ski[:8], err)
                delay = min(delay * 2, 60.0)
            await asyncio.sleep(delay)

    def set_heartbeat(self, on: bool) -> None:
        """Send heartbeats only while the wallbox is there, so energy managers notice when it is not."""
        self._heartbeat_on = on
        if not self._started:
            return
        if on:
            self.device.start()
        else:
            self.device.stop()

    # --- structure -------------------------------------------------------------------------------

    def apply_structure(self, profile: Profile, announce: bool = True) -> None:
        """Make entities, features and use cases match ``profile`` (data is copied for new entities)."""
        device = self.device
        old = {tuple(e["address"]): e for e in (self.profile.entities if self.profile else [])}
        new = {tuple(e["address"]): e for e in profile.entities}
        for address, entity in old.items():
            changed = address not in new or Profile.entity_structure(entity) != Profile.entity_structure(new[address])
            if changed and (local := device.entity(address)) is not None:
                device.remove_entity(local)
        for address, entity in new.items():
            if device.entity(address) is None:
                self._add_entity(entity, announce)
        if self.virtual_ev is not None:
            self.virtual_ev.ensure(announce)
        self._set_use_cases(profile)
        self.profile = profile
        if self._started:
            self.set_heartbeat(self._heartbeat_on)

    def _add_entity(self, spec: dict[str, Any], announce: bool) -> LocalEntity:
        entity = self.device.add_entity(spec["type"], tuple(spec["address"]), announce=False)
        entity.description = spec.get("description")
        for f in sorted(spec["features"], key=lambda f: f["id"]):
            feature = LocalFeature(entity, f["id"], f["type"], f["role"], f.get("description"))
            entity.features.append(feature)
            for fn, (read, write) in f["functions"].items():
                feature.add_function(fn, read=read, write=write)
            for fn, data in (f.get("data") or {}).items():
                if fn in feature.operations and fn != FN_HEARTBEAT:
                    feature.data[fn] = copy.deepcopy(data)
            if feature.role == Role.SERVER:
                feature.write_approval = lambda msg, feature=feature: self._approve(feature, msg)
        entity._next_id = max((f.id for f in entity.features), default=0) + 1
        if announce:
            self.device.announce_entity(entity)
        return entity

    def _set_use_cases(self, profile: Profile) -> None:
        device = self.device
        infos = []
        extra = self.virtual_ev.use_cases() if self.virtual_ev is not None else []
        for uc in [*profile.use_cases, *extra]:
            entity = list(uc.get("entity") or [])
            if entity and device.entity(entity) is None:
                continue
            address: dict[str, Any] = {"device": device.address}
            if entity:
                address["entity"] = entity
            infos.append({"address": address, "actor": uc.get("actor"),
                          "useCaseSupport": copy.deepcopy(uc.get("support") or [])})
        new = {"useCaseInformation": infos}
        if device.node_management.data.get(FN_USE_CASE) != new:
            device.node_management.set_data(FN_USE_CASE, new)

    # --- data ----------------------------------------------------------------------------------------

    def feature(self, address: tuple[int, ...], feature_id: int) -> LocalFeature | None:
        entity = self.device.entity(address)
        return entity.feature_by_id(feature_id) if entity else None

    def mirror(self, address: tuple[int, ...], feature_id: int, function: str, data: Any) -> None:
        """The wallbox reported new data: pass it on."""
        feature = self.feature(address, feature_id)
        if (feature is None or feature.role != Role.SERVER or function not in feature.operations
                or function == FN_HEARTBEAT or (address, feature_id, function) in self.owned):
            return
        if feature.data.get(function) != data:
            feature.set_data(function, data)

    def add_virtual_ev(self, **options: Any) -> VirtualEV:
        """Show energy managers an EV entity under the EVSE (the wallbox has none)."""
        self.virtual_ev = VirtualEV(self, **options)
        self.virtual_ev.ensure(announce=self._started)
        if self.profile is not None:
            self._set_use_cases(self.profile)
        return self.virtual_ev

    def remove_virtual_ev(self) -> None:
        ev, self.virtual_ev = self.virtual_ev, None
        if ev is not None and ev.entity is not None and ev.entity in self.device.entities:
            self.device.remove_entity(ev.entity)
        if self.profile is not None:
            self._set_use_cases(self.profile)

    def mirror_manufacturer(self, data: Any) -> None:
        feature = self.device.entity((0,)).feature("DeviceClassification", Role.SERVER)
        if data and feature.data.get(FN_MANUFACTURER) != data:
            feature.set_data(FN_MANUFACTURER, data)

    def _approve(self, feature: LocalFeature, msg: Message) -> SpineError | None:
        return self.on_write(feature, msg) if self.on_write else None

    # --- energy managers ---------------------------------------------------------------------------

    def label(self, ski: str) -> str:
        manager = self.managers.get(ski)
        return manager.label if manager else ski[:8]

    def _client(self, feature_type: str) -> LocalFeature | None:
        for entity in self.device.entities:
            if (f := entity.feature(feature_type, Role.CLIENT)) is not None:
                return f
        return None

    def _emit(self, ski: str, what: str) -> None:
        if self.on_hems_event:
            try:
                self.on_hems_event(ski, what)
            except Exception:
                _LOGGER.exception("energy manager event handler failed")

    def _on_event(self, event: Event) -> None:
        if event.type == EventType.DEVICE and event.change == Change.ADD:
            self.managers[event.ski] = EnergyManager(event.ski)
            _LOGGER.info("energy manager %s connected (%s, %s)", event.ski[:8],
                         event.device.device_type if event.device else "?",
                         event.device.address if event.device else "?")
            self._emit(event.ski, "connected")
        elif event.type == EventType.DEVICE and event.change == Change.REMOVE:
            manager = self.managers.pop(event.ski, None)
            _LOGGER.info("energy manager %s disconnected", manager.label if manager else event.ski[:8])
            self._emit(event.ski, "disconnected")
        elif event.type == EventType.ENTITY and event.change == Change.ADD and event.entity is not None:
            entity = event.entity
            remote = entity.feature("DeviceDiagnosis", Role.SERVER)
            local = self._client("DeviceDiagnosis")
            if remote is not None and local is not None and FN_HEARTBEAT in remote.operations:
                spawn(self._quietly(local.subscribe(remote)))
                local.read(remote, FN_HEARTBEAT)
            remote = entity.feature("DeviceClassification", Role.SERVER)
            local = self._client("DeviceClassification")
            if remote is not None and local is not None and FN_MANUFACTURER in remote.operations:
                local.read(remote, FN_MANUFACTURER)
        elif event.type == EventType.DATA and event.ski in self.managers and event.feature is not None \
                and event.local_feature is not None and event.local_feature.role == Role.CLIENT:
            manager = self.managers[event.ski]
            if event.function == FN_HEARTBEAT:
                manager.last_heartbeat = time.monotonic()
                if manager.heartbeat_lost:
                    manager.heartbeat_lost = False
                    _LOGGER.info("heartbeat of energy manager %s is back", manager.label)
                    self._emit(event.ski, "heartbeat_resumed")
            elif event.function == FN_MANUFACTURER and isinstance(event.data, dict):
                d = event.data
                manager.name = " ".join(x for x in (d.get("brandName") or d.get("vendorName"),
                                                    d.get("deviceName")) if x)
                _LOGGER.info("energy manager %s: %s", event.ski[:8], json.dumps(d, ensure_ascii=False))

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for ski, manager in list(self.managers.items()):
                if manager.last_heartbeat is not None and not manager.heartbeat_lost \
                        and now - manager.last_heartbeat > HEARTBEAT_LOSS:
                    manager.heartbeat_lost = True
                    _LOGGER.warning("no heartbeat from energy manager %s for %.0f s", manager.label,
                                    now - manager.last_heartbeat)
                    self._emit(ski, "heartbeat_lost")

    @staticmethod
    async def _quietly(coro) -> None:
        with contextlib.suppress(SpineError, TimeoutError):
            await coro

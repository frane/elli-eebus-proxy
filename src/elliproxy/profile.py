"""The device the proxy shows to energy managers: a copy of the Elli's EEBUS device.

A :class:`Profile` holds the Elli's entities, features (with their functions
and last known data) and use cases. It is taken from the live wallbox, cached
in ``profile.json`` (so the proxy can start while the wallbox is offline) or,
without any wallbox, built from the simulated Elli Charger 2.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pyeebus.spine import LocalDevice, RemoteDevice, Role
from pyeebus.spine.device import FN_HEARTBEAT, FN_MANUFACTURER, FN_USE_CASE
from pyeebus.spine.model import as_list, parse_duration

_LOGGER = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_TIMEOUT = 60.0  # what the Elli Charger 2 announces (PT1M)


@dataclass
class Identity:
    """How the device announces itself via mDNS and SPINE."""

    brand: str = "Elli"
    model: str = "Elli"
    serial: str = ""
    vendor: str | None = None
    ship_id: str = ""
    device_type: str = "ChargingStation"


@dataclass
class Profile:
    device_address: str
    device_type: str = "ChargingStation"
    feature_set: str = "smart"
    identity: Identity = field(default_factory=Identity)
    manufacturer: dict[str, Any] | None = None  # DeviceClassification data of entity [0]
    entities: list[dict[str, Any]] = field(default_factory=list)
    use_cases: list[dict[str, Any]] = field(default_factory=list)
    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT

    # --- (de)serialisation ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        data = dict(data)
        data["identity"] = Identity(**(data.get("identity") or {}))
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Profile | None:
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("ignoring %s: %s", path, err)
            return None

    # --- structure -----------------------------------------------------------------------

    @staticmethod
    def entity_structure(entity: dict[str, Any]) -> tuple:
        return (tuple(entity["address"]), entity["type"], tuple(
            (f["id"], f["type"], f["role"], tuple(sorted((fn, tuple(ops)) for fn, ops in f["functions"].items())))
            for f in entity["features"]))

    def structure(self) -> tuple:
        """Entities and features without data: if this changes, the HEMS must be told."""
        return tuple(self.entity_structure(e) for e in self.entities)

    def entity(self, address: tuple[int, ...] | list[int]) -> dict[str, Any] | None:
        return next((e for e in self.entities if tuple(e["address"]) == tuple(address)), None)

    # --- sources ---------------------------------------------------------------------------

    @classmethod
    def from_remote(cls, remote: RemoteDevice, mdns: Identity | None = None) -> Profile:
        """Copy a connected device (the wallbox)."""
        entities = []
        manufacturer = None
        for entity in remote.entities:
            features = []
            for f in entity.features:
                if entity.entity == (0,):
                    if f.type == "DeviceClassification" and f.role == Role.SERVER:
                        manufacturer = f.get(FN_MANUFACTURER)
                    continue
                features.append({
                    "id": f.id, "type": f.type, "role": f.role, "description": f.description,
                    "functions": {fn: [ops.read, ops.write] for fn, ops in f.operations.items()},
                    "data": copy.deepcopy(f.data) if f.role == Role.SERVER else {},
                })
            if entity.entity != (0,):
                entities.append({"address": list(entity.entity), "type": entity.type,
                                 "description": entity.description, "features": features})
        use_cases = [
            {"actor": info.get("actor"), "entity": as_list((info.get("address") or {}).get("entity")),
             "support": as_list(info.get("useCaseSupport"))}
            for info in remote.use_cases()]
        profile = cls(device_address=remote.address or "", device_type=remote.device_type or "ChargingStation",
                      feature_set=remote.feature_set or "smart", manufacturer=manufacturer,
                      entities=entities, use_cases=use_cases)
        profile.heartbeat_timeout = profile._heartbeat_timeout()
        profile.identity = mdns or profile.guess_identity()
        profile.identity.device_type = profile.device_type
        return profile

    @classmethod
    def from_local(cls, device: LocalDevice) -> Profile:
        """Copy a local device (used for the built-in Elli Charger 2 profile)."""
        entities = []
        for entity in device.entities:
            if entity.entity == (0,):
                continue
            entities.append({
                "address": list(entity.entity), "type": entity.type, "description": entity.description,
                "features": [{
                    "id": f.id, "type": f.type, "role": f.role, "description": f.description,
                    "functions": {fn: [ops.read, ops.write] for fn, ops in f.operations.items()},
                    "data": copy.deepcopy(f.data)} for f in entity.features]})
        nm_data = device.node_management.get(FN_USE_CASE) or {}
        use_cases = [
            {"actor": info.get("actor"), "entity": as_list((info.get("address") or {}).get("entity")),
             "support": as_list(info.get("useCaseSupport"))}
            for info in as_list(nm_data.get("useCaseInformation"))]
        profile = cls(device_address=device.address, device_type=device.device_type,
                      feature_set=device.feature_set, entities=entities, use_cases=use_cases,
                      heartbeat_timeout=device.heartbeat_timeout)
        profile.identity = profile.guess_identity()
        return profile

    @classmethod
    def builtin(cls, serial: str = "00099999") -> Profile:
        """The Elli Charger 2 as recorded from real hardware (see elli-eebus' simulator)."""
        from ellieebus.simulator import SimulatedElli
        from pyeebus.ship import Identity as ShipIdentity

        sim = SimulatedElli(ShipIdentity.create("builtin"), serial=serial, quirks=False,
                            announce=False, discover=False)
        profile = cls.from_local(sim.device)
        profile.identity = Identity(brand="Elli", model="Elli", serial=serial, vendor="3210",
                                    ship_id=sim.service.ship_id)
        return profile

    # --- helpers -----------------------------------------------------------------------------

    def _heartbeat_timeout(self) -> float:
        for entity in self.entities:
            for f in entity["features"]:
                hb = (f.get("data") or {}).get(FN_HEARTBEAT) or {}
                if f["type"] == "DeviceDiagnosis" and hb.get("heartbeatTimeout"):
                    try:
                        return parse_duration(hb["heartbeatTimeout"])
                    except ValueError:
                        pass
        return DEFAULT_HEARTBEAT_TIMEOUT

    def manufacturer_data(self) -> dict[str, Any]:
        if self.manufacturer:
            return self.manufacturer
        for entity in self.entities:
            for f in entity["features"]:
                if data := (f.get("data") or {}).get(FN_MANUFACTURER):
                    return data
        return {}

    def guess_identity(self) -> Identity:
        """Identity from SPINE data (until the wallbox's mDNS announcement was seen)."""
        m = self.manufacturer_data()
        brand = m.get("brandName") or m.get("vendorName") or "Elli"
        serial = m.get("serialNumber") or ""
        vendor = m.get("vendorCode")
        model = "Elli"
        # device address d:_i:<vendor>_<model>-<serial>
        if match := re.match(r"^d:_[in]:([^_]+)_(.+)$", self.device_address or ""):
            vendor = vendor or match.group(1)
            rest = match.group(2)
            model = rest[: -len(serial) - 1] if serial and rest.endswith(f"-{serial}") else rest
        return Identity(brand=brand, model=model, serial=serial, vendor=vendor,
                        ship_id=f"{brand}-Wallbox-{serial}" if serial else f"{brand}-Wallbox",
                        device_type=self.device_type)

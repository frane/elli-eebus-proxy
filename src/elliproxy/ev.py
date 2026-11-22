"""A virtual EV entity for energy managers that control wallboxes only through the EV.

The Elli Charger 2 (current firmware) offers power limitation (LPC) on its
EVSE entity but no EV entity. Energy managers such as Solar Manager control
wallboxes with the EV charging use cases instead: current limits per phase on
the EV entity (OPEV: obligation, OSCEV: recommendation). The proxy shows them
an EV entity ``[1, 1]`` under the EVSE and turns their current limits into a
power limit for the wallbox (A x 230 V x phases).

The Elli Charger 2 does not report whether a car is plugged in, so the EV
entity is always there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyeebus.spine import LocalEntity, Role
from pyeebus.spine.model import as_list, scaled_number, scaled_value

if TYPE_CHECKING:
    from .hems import HemsSide

_LOGGER = logging.getLogger(__name__)

NOMINAL_VOLTAGE = 230.0
FN_LIMITS = "loadControlLimitListData"
PHASES = ("a", "b", "c")
USE_CASES = (
    ("evCommissioningAndConfiguration", "1.0.1", [1, 2, 3, 4, 5, 6, 7, 8]),
    ("measurementOfElectricityDuringEvCharging", "1.0.1", [1, 2, 3]),
    ("overloadProtectionByEvChargingCurrentCurtailment", "1.0.1", [1, 2, 3]),
    ("optimizationOfSelfConsumptionDuringEvCharging", "1.0.1", [1, 2, 3]),
)


def _sn(value: float) -> dict[str, int]:
    return scaled_number(value)


def _scaled(value: float, digits: int = 1) -> dict[str, int]:
    return scaled_number(round(value, digits))


class VirtualEV:
    def __init__(self, hems: HemsSide, evse_address: tuple[int, ...] = (1,), *, min_current: float = 6.0,
                 max_current: float = 16.0, phases: int = 3) -> None:
        self.hems = hems
        self.evse_address = tuple(evse_address)
        self.address = (*self.evse_address, 1)
        self.min_current, self.max_current, self.phases = min_current, max_current, phases
        self.entity: LocalEntity | None = None
        self._parent: LocalEntity | None = None
        self._power = 0.0

    # --- entity ----------------------------------------------------------------------------

    def ensure(self, announce: bool = True) -> None:
        """Add the EV entity under the EVSE (again, if the EVSE was rebuilt)."""
        device = self.hems.device
        parent = device.entity(self.evse_address)
        if self.entity is not None and (parent is None or parent is not self._parent):
            if self.entity in device.entities:
                device.remove_entity(self.entity)
            self.entity = None
        if parent is None or self.entity is not None:
            return
        self._parent = parent
        self.entity = self._build()
        if announce:
            device.announce_entity(self.entity)

    def use_cases(self) -> list[dict[str, Any]]:
        if self.entity is None:
            return []
        return [{"actor": "EV", "entity": list(self.address), "support": [
            {"useCaseName": name, "useCaseVersion": version, "useCaseAvailable": True,
             "scenarioSupport": scenarios, "useCaseDocumentSubRevision": "release"}
            for name, version, scenarios in USE_CASES]}]

    def is_mine(self, feature) -> bool:
        return self.entity is not None and feature.entity is self.entity

    def _build(self) -> LocalEntity:
        ev = self.hems.device.add_entity("EV", self.address, announce=False)
        ev.description = "Electric Vehicle"
        phases = PHASES[: self.phases]

        f = ev.add_feature("DeviceClassification", Role.SERVER)
        f.add_function("deviceClassificationManufacturerData")
        f.data["deviceClassificationManufacturerData"] = {"deviceName": "EV"}

        f = ev.add_feature("DeviceDiagnosis", Role.SERVER)
        f.add_function("deviceDiagnosisStateData")
        f.data["deviceDiagnosisStateData"] = {"operatingState": "normalOperation"}

        f = ev.add_feature("DeviceConfiguration", Role.SERVER)
        f.add_function("deviceConfigurationKeyValueDescriptionListData")
        f.add_function("deviceConfigurationKeyValueListData")
        f.data["deviceConfigurationKeyValueDescriptionListData"] = {"deviceConfigurationKeyValueDescriptionData": [
            {"keyId": 1, "keyName": "communicationsStandard", "valueType": "string"},
            {"keyId": 2, "keyName": "asymmetricChargingSupported", "valueType": "boolean"}]}
        f.data["deviceConfigurationKeyValueListData"] = {"deviceConfigurationKeyValueData": [
            {"keyId": 1, "value": {"string": "iec61851"}, "isValueChangeable": False},
            {"keyId": 2, "value": {"boolean": False}, "isValueChangeable": False}]}

        f = ev.add_feature("ElectricalConnection", Role.SERVER)
        for fn in ("electricalConnectionDescriptionListData", "electricalConnectionParameterDescriptionListData",
                   "electricalConnectionPermittedValueSetListData"):
            f.add_function(fn)
        f.data["electricalConnectionDescriptionListData"] = {"electricalConnectionDescriptionData": [
            {"electricalConnectionId": 0, "powerSupplyType": "ac", "acConnectedPhases": self.phases,
             "positiveEnergyDirection": "consume"}]}
        params, permitted = [], []
        for i, phase in enumerate(phases):
            params.append({"electricalConnectionId": 0, "parameterId": i + 1, "measurementId": i + 1,
                           "voltageType": "ac", "acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral",
                           "acMeasurementType": "real", "acMeasurementVariant": "rms"})
            permitted.append({"electricalConnectionId": 0, "parameterId": i + 1, "permittedValueSet": [
                {"value": [_sn(0)], "range": [{"min": _sn(self.min_current), "max": _sn(self.max_current)}]}]})
            params.append({"electricalConnectionId": 0, "parameterId": i + 4, "measurementId": i + 4,
                           "voltageType": "ac", "acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral",
                           "acMeasurementType": "real", "acMeasurementVariant": "rms"})
        params.append({"electricalConnectionId": 0, "parameterId": 7, "scopeType": "acPowerTotal"})
        permitted.append({"electricalConnectionId": 0, "parameterId": 7, "permittedValueSet": [
            {"value": [_sn(0)], "range": [
                {"min": _sn(self.min_current * NOMINAL_VOLTAGE * self.phases),
                 "max": _sn(self.max_current * NOMINAL_VOLTAGE * self.phases)}]}]})
        f.data["electricalConnectionParameterDescriptionListData"] = {
            "electricalConnectionParameterDescriptionData": params}
        f.data["electricalConnectionPermittedValueSetListData"] = {
            "electricalConnectionPermittedValueSetData": permitted}

        f = ev.add_feature("Measurement", Role.SERVER)
        for fn in ("measurementDescriptionListData", "measurementConstraintsListData", "measurementListData"):
            f.add_function(fn)
        descs = []
        for i, _phase in enumerate(phases):
            descs.append({"measurementId": i + 1, "measurementType": "current", "commodityType": "electricity",
                          "unit": "A", "scopeType": "acCurrent"})
            descs.append({"measurementId": i + 4, "measurementType": "power", "commodityType": "electricity",
                          "unit": "W", "scopeType": "acPower"})
        f.data["measurementDescriptionListData"] = {"measurementDescriptionData": descs}
        f.data["measurementConstraintsListData"] = {}
        f.data["measurementListData"] = {"measurementData": self._measurements(self._power)}

        f = ev.add_feature("LoadControl", Role.SERVER)
        f.add_function("loadControlLimitDescriptionListData")
        f.add_function("loadControlLimitConstraintsListData")
        f.add_function(FN_LIMITS, read=True, write=True)
        limit_descs, limits = [], []
        for i, _phase in enumerate(phases):
            limit_descs.append({"limitId": i + 1, "limitType": "maxValueLimit", "limitCategory": "obligation",
                                "measurementId": i + 1, "unit": "A", "scopeType": "overloadProtection"})
            limit_descs.append({"limitId": i + 4, "limitType": "maxValueLimit", "limitCategory": "recommendation",
                                "measurementId": i + 1, "unit": "A", "scopeType": "selfConsumption"})
            limits.append({"limitId": i + 1, "isLimitChangeable": True, "isLimitActive": False,
                           "value": _sn(self.max_current)})
            limits.append({"limitId": i + 4, "isLimitChangeable": True, "isLimitActive": False,
                           "value": _sn(self.max_current)})
        f.data["loadControlLimitDescriptionListData"] = {"loadControlLimitDescriptionData": limit_descs}
        f.data["loadControlLimitConstraintsListData"] = {}
        f.data[FN_LIMITS] = {"loadControlLimitData": sorted(limits, key=lambda x: x["limitId"])}

        for feature in ev.features:
            feature.write_approval = lambda msg, feature=feature: self.hems._approve(feature, msg)
        return ev

    # --- measurements ------------------------------------------------------------------------

    def _measurements(self, power: float) -> list[dict[str, Any]]:
        per_phase = power / self.phases
        out = []
        for i in range(self.phases):
            out.append({"measurementId": i + 1, "valueType": "value", "value": _scaled(per_phase / NOMINAL_VOLTAGE),
                        "valueSource": "calculatedValue"})
            out.append({"measurementId": i + 4, "valueType": "value", "value": _scaled(per_phase),
                        "valueSource": "calculatedValue"})
        return sorted(out, key=lambda x: x["measurementId"])

    def update_power(self, watts: float | None) -> None:
        """The wallbox's total power: shown as current and power per phase (calculated)."""
        watts = watts or 0.0
        if watts == self._power:
            return
        self._power = watts
        if self.entity is not None:
            self.entity.feature("Measurement", Role.SERVER).set_data(
                "measurementListData", {"measurementData": self._measurements(watts)})

    # --- limits --------------------------------------------------------------------------------

    def power_limit(self, data: dict[str, Any]) -> float | None:
        """The power limit (W) that the current limits in ``data`` stand for, None for none.

        Obligation (OPEV) and recommendation (OSCEV): the lowest active one counts.
        """
        currents = [scaled_value(item.get("value")) for item in as_list(data.get("loadControlLimitData"))
                    if item.get("isLimitActive") and item.get("value") is not None]
        currents = [c for c in currents if c is not None]
        if not currents:
            return None
        return min(currents) * NOMINAL_VOLTAGE * self.phases

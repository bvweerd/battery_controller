"""Diagnostics support for the Heating Curve Optimizer integration."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

TO_REDACT: set[str] = set()


def _serialize_state(state: State | None) -> dict[str, Any]:
    """Serialize a Home Assistant state for diagnostics output."""

    if state is None:
        return {}

    return {
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
        "context": {
            "id": state.context.id,
            "parent_id": state.context.parent_id,
            "user_id": state.context.user_id,
        },
    }


def _serialize_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a serialisable copy of a mapping."""

    if not data:
        return {}

    return {key: value for key, value in data.items()}


def _stringify(value: Any) -> Any:
    """Convert enums to their value for diagnostics."""

    if isinstance(value, Enum):
        return value.value
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics information for a config entry."""

    domain_data = hass.data.get(DOMAIN, {})
    stored_entry_data = domain_data.get(entry.entry_id, {})
    entity_map = domain_data.get("entities", {})
    ent_reg = er.async_get(hass)

    sensors: list[dict[str, Any]] = []
    for ent_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent_entry.domain != "sensor":
            continue

        entity = entity_map.get(ent_entry.entity_id)
        state = hass.states.get(ent_entry.entity_id)
        extra_attrs: Mapping[str, Any] | None = None
        if entity is not None and hasattr(entity, "extra_state_attributes"):
            attrs = entity.extra_state_attributes  # type: ignore[attr-defined]
            if isinstance(attrs, Mapping):
                extra_attrs = attrs

        sensors.append(
            {
                "entity_id": ent_entry.entity_id,
                "unique_id": ent_entry.unique_id,
                "original_name": ent_entry.original_name,
                "name": getattr(entity, "name", None),
                "available": getattr(entity, "available", None) if entity else None,
                "native_unit_of_measurement": getattr(
                    entity, "native_unit_of_measurement", None
                )
                if entity
                else None,
                "native_value": getattr(entity, "native_value", None)
                if entity
                else None,
                "device_class": _stringify(getattr(entity, "device_class", None))
                if entity
                else ent_entry.device_class,
                "state_class": _stringify(getattr(entity, "state_class", None))
                if entity
                else ent_entry.capabilities.get("state_class")
                if ent_entry.capabilities
                else None,
                "state": _serialize_state(state),
                "extra_state_attributes": _serialize_mapping(extra_attrs),
            }
        )

    diagnostics: dict[str, Any] = {
        "config_entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "stored_data": _serialize_mapping(stored_entry_data),
        "sensors": sensors,
    }

    return diagnostics

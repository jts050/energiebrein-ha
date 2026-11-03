"""Lees de gekoppelde thermostaat en warmwaterfunctie via Home Assistant."""
import math
import re


def entiteiten(thermostaat):
    if not re.fullmatch(r"climate\.[a-z0-9_]+", thermostaat or ""):
        raise ValueError("Koppel eerst een thermostaat in Home Assistant")
    return thermostaat, "water_heater." + thermostaat.split(".", 1)[1]


def getal(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def overzicht(states, thermostaat):
    huis, water = entiteiten(thermostaat)
    prefix = huis.split(".", 1)[1]
    rows = {s["entity_id"]: s for s in states}
    result = {"entiteiten": []}
    for key, entity in (("huis", huis), ("water", water)):
        state = rows.get(entity)
        if not state:
            result[key] = None
            continue
        attrs = state.get("attributes", {})
        available = state.get("state") not in ("unknown", "unavailable")
        supported = int(attrs.get("supported_features", 0))
        result[key] = {
            "entity": entity, "beschikbaar": available,
            "huidig": getal(attrs.get("current_temperature")) if available else None,
            "doel": getal(attrs.get("temperature")) if available else None,
            "min": getal(attrs.get("min_temp")), "max": getal(attrs.get("max_temp")),
            "stap": getal(attrs.get("target_temp_step")) or (0.5 if key == "huis" else 1),
            "instelbaar": available and bool(supported & 1),
            "stand": attrs.get("hvac_action") or state.get("state"),
        }
    for entity, state in rows.items():
        domain, object_id = entity.split(".", 1)
        if domain not in ("sensor", "binary_sensor"):
            continue
        if object_id != prefix and not object_id.startswith(prefix + "_"):
            continue
        attrs = state.get("attributes", {})
        result["entiteiten"].append({"entity": entity,
            "naam": attrs.get("friendly_name", entity), "waarde": state.get("state"),
            "eenheid": attrs.get("unit_of_measurement", "")})
    return result


def controleer_temperatuur(data, deel, temperatuur):
    if deel not in ("huis", "water"):
        raise ValueError("Kies huis of water")
    item = data.get(deel)
    if not item or not item["instelbaar"]:
        raise ValueError("Deze temperatuur is niet beschikbaar om in te stellen")
    value = getal(temperatuur)
    lo, hi, step = item["min"], item["max"], item["stap"]
    if value is None or lo is None or hi is None or not lo <= value <= hi:
        raise ValueError("Temperatuur valt buiten de grenzen van de installatie")
    if abs((value - lo) / step - round((value - lo) / step)) > 0.00001:
        raise ValueError("Gebruik de temperatuurstappen van de installatie")
    return item["entity"], value

"""Energiebrein-gegevens als entiteiten in Home Assistant, zodat je ze op je eigen dashboard kunt zetten.

Draait Energiebrein als add-on, dan geeft Home Assistant het een sleutel (SUPERVISOR_TOKEN). Daarmee zet het brein elke 30 seconden
een aantal sensoren neer, zoals sensor.energiebrein_zon (kW) en sensor.energiebrein_accu_lading (%). Ze staan onder Instellingen →
Entiteiten niet in het register (ze hebben geen eigen apparaat), maar zijn overal te kiezen, ook in kaarten en automatiseringen.
Na een herstart van Home Assistant zijn ze binnen een halve minuut weer terug. Buiten Home Assistant doet deze module niets."""
from __future__ import annotations

import asyncio
import logging

from . import telefoon

log = logging.getLogger("ha_entiteiten")
INTERVAL = 30

# naam: (vriendelijke naam, eenheid, device_class, icoon)
_KW = ("kW", "power")
_EUR = ("€", "monetary")


def _s(naam, eenheid=None, klasse=None, icoon=None, meten=True):
    return {"naam": naam, "eenheid": eenheid, "klasse": klasse, "icoon": icoon, "meten": meten}


def _num(x, nd=3):
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def bouw(st: dict) -> dict[str, dict]:
    """Maakt van de status van het brein de lijst entiteiten: {entity_id: {"state", "attributes"}}. Onbekende waarden ontbreken."""
    m, b, v = st.get("meting") or {}, st.get("besluit") or {}, st.get("vandaag") or {}
    ps = st.get("plan_samenvatting") or {}
    waarden: list[tuple[str, object, dict]] = [
        ("zon", _num(m.get("zon_kw")), _s("Energiebrein zon", "kW", "power", "mdi:solar-power")),
        ("net", _num(m.get("net_kw")), _s("Energiebrein net (+ afname, − teruglevering)", "kW", "power", "mdi:transmission-tower")),
        ("huis", _num(m.get("huis_kw")), _s("Energiebrein huis", "kW", "power", "mdi:home-lightning-bolt")),
        ("accu_vermogen", _num(m.get("accu_kw")), _s("Energiebrein accu (+ laden, − leveren)", "kW", "power", "mdi:battery-charging")),
        ("accu_lading", _num(m.get("accu_soc"), 1), _s("Energiebrein accu lading", "%", "battery", "mdi:battery")),
        ("auto", _num(m.get("auto_kw")), _s("Energiebrein auto laden", "kW", "power", "mdi:ev-station")),
        ("warmtepomp", _num(m.get("wp_kw")), _s("Energiebrein warmtepomp", "kW", "power", "mdi:heat-pump")),
        ("warmtepomp_stand", m.get("wp_stand"), _s("Energiebrein warmtepomp stand", icoon="mdi:heat-pump-outline", meten=False)),
        ("inkoopprijs", _num(b.get("inkoop"), 4), _s("Energiebrein inkoopprijs", "€/kWh", None, "mdi:currency-eur")),
        ("terugleverprijs", _num(b.get("teruglever"), 4), _s("Energiebrein terugleverprijs", "€/kWh", None, "mdi:currency-eur")),
        ("accu_actie", b.get("accu"), _s("Energiebrein accu actie", icoon="mdi:battery-sync", meten=False)),
        ("laadpaal_actie", b.get("laadpaal"), _s("Energiebrein laadpaal actie", icoon="mdi:ev-plug-type2", meten=False)),
        ("reden", b.get("reden"), _s("Energiebrein reden", icoon="mdi:head-lightbulb-outline", meten=False)),
        ("kosten_vandaag", _num(v.get("kosten"), 2), _s("Energiebrein kosten vandaag", "€", "monetary", "mdi:cash", meten=False)),
        ("bespaard_vandaag", _num(v.get("bespaard_accu"), 2), _s("Energiebrein bespaard door accu vandaag", "€", "monetary", "mdi:piggy-bank", meten=False)),
        ("afname_vandaag", _num(v.get("afname_kwh"), 2), _s("Energiebrein afname vandaag", "kWh", "energy", "mdi:transmission-tower-export", meten=False)),
        ("teruglevering_vandaag", _num(v.get("teruglevering_kwh"), 2), _s("Energiebrein teruglevering vandaag", "kWh", "energy", "mdi:transmission-tower-import", meten=False)),
        ("zon_vandaag", _num(v.get("zon_kwh"), 2), _s("Energiebrein zon vandaag", "kWh", "energy", "mdi:solar-power-variant", meten=False)),
        ("meldingen", len([x for x in (st.get("storingen") or []) if x.get("ernst") != "info"]),
         _s("Energiebrein meldingen", icoon="mdi:alert-circle-outline", meten=False)),
    ]
    uit = {}
    for sleutel, waarde, d in waarden:
        if waarde is None or waarde == "":
            continue
        a = {"friendly_name": d["naam"], "attribution": "Energiebrein"}
        if d["eenheid"]:
            a["unit_of_measurement"] = d["eenheid"]
        if d["klasse"]:
            a["device_class"] = d["klasse"]
        if d["icoon"]:
            a["icon"] = d["icoon"]
        if d["eenheid"] in ("kW", "%") and d["meten"]:
            a["state_class"] = "measurement"
        if isinstance(waarde, str):
            waarde = waarde[:250]                     # Home Assistant staat maximaal 255 tekens toe
        uit[f"sensor.energiebrein_{sleutel}"] = {"state": waarde, "attributes": a}
    return uit


async def zet(entiteiten: dict[str, dict]) -> int:
    """Stuurt de entiteiten naar Home Assistant. Geeft het aantal dat gelukt is."""
    from .net import sessie
    gelukt = 0
    async with sessie(15) as s:
        for eid, e in entiteiten.items():
            async with s.post(f"{telefoon.HA}/states/{eid}", headers=telefoon._kop(), json=e) as r:
                if r.status < 300:
                    gelukt += 1
                else:
                    log.warning("Home Assistant weigert %s (%s)", eid, r.status)
    return gelukt


async def lus(brein) -> None:
    """Blijft draaien en ververst de entiteiten. Zet je `home_assistant: entiteiten: uit`, dan doet het niets."""
    stand = str(((brein.cfg.get("home_assistant") or {}).get("entiteiten", "aan"))).lower()
    if not telefoon.beschikbaar() or stand in ("uit", "false", "nee", "0"):
        return
    fout = None
    while True:
        try:
            n = await zet(bouw(brein.status()))
            if fout:
                log.info("Entiteiten in Home Assistant weer bijgewerkt (%d)", n)
            fout = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if str(e) != fout:                         # elke fout maar één keer in het log
                log.warning("Entiteiten in Home Assistant bijwerken mislukt: %s", e)
            fout = str(e)
        await asyncio.sleep(INTERVAL)

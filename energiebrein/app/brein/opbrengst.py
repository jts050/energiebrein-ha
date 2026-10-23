"""Wat het je oplevert: per dag gerekend met je eigen metingen en de prijs van elk kwartier.

Drie vergelijkingen:
- besparing totaal: wat je had betaald als je al je stroom van het net haalde (geen zon, geen accu), min wat je nu betaalt
- door de accu: wat je had betaald zonder accu (wel zon), min wat je nu betaalt
- teruglevering: wat je kreeg voor stroom die naar het net ging
"""
from __future__ import annotations
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from .prijzen import model_prijs


def opbrengst(rijen: list[dict], prijzen, dagen: int = 30, accu_kwh: float = 0.0, eta: float = 0.95) -> dict:
    """accu_kwh en eta: grootte en ontlaadrendement van de accu. Stroom die op een dag in de accu is gegaan maar nog niet is
    gebruikt (of andersom), telt mee tegen de gemiddelde prijs van die dag. Anders lijkt een dag waarop 's nachts goedkoop
    is geladen voor de volgende ochtend een verliesdag, en de dag erna een topdag."""
    per_dag: "OrderedDict[str, dict]" = OrderedDict()
    vorige_soc = None
    for r in rijen:
        if (r.get("n") or 0) < 3:
            if r.get("soc") is not None:
                vorige_soc = float(r["soc"])
            continue
        t = datetime.fromisoformat(r["start"]) if isinstance(r["start"], str) else r["start"]
        dag = t.astimezone().date().isoformat()
        beurs = r.get("beurs")
        if beurs is None:
            beurs = model_prijs(t)
        inkoop, terug = prijzen.inkoop(beurs, t), prijzen.teruglever(beurs, t)
        verbruik = (r.get("huis_kwh") or 0) + (r.get("wp_kwh") or 0) + (r.get("auto_kwh") or 0)
        d = per_dag.get(dag)
        if d is None:
            d = per_dag[dag] = {"dag": dag, "kosten": 0.0, "zonder_accu": 0.0, "zonder_alles": 0.0, "teruglevering": 0.0,
                                "zon_kwh": 0.0, "_prijs": 0.0, "_n": 0, "_soc0": vorige_soc, "_soc1": None}
        d["kosten"] += r.get("kosten") or 0
        d["zonder_accu"] += r.get("kosten_zonder") or 0
        d["zonder_alles"] += verbruik * inkoop
        d["teruglevering"] += (r.get("terug_kwh") or 0) * terug
        d["zon_kwh"] += r.get("zon_kwh") or 0
        d["_prijs"] += inkoop
        d["_n"] += 1
        if r.get("soc") is not None:
            if d["_soc0"] is None:
                d["_soc0"] = float(r["soc"])
            d["_soc1"] = vorige_soc = float(r["soc"])
    lijst = list(per_dag.values())[-dagen:]
    for d in lijst:
        lading = 0.0
        if accu_kwh and d["_soc0"] is not None and d["_soc1"] is not None and d["_n"]:
            lading = (d["_soc1"] - d["_soc0"]) / 100 * accu_kwh * eta * d["_prijs"] / d["_n"]
        d["lading_eur"] = lading
        d["besparing"] = d["zonder_alles"] - d["kosten"] + lading
        d["door_accu"] = d["zonder_accu"] - d["kosten"] + lading
        for k in [k for k in d if k.startswith("_")]:
            del d[k]
        for k in list(d):
            if k != "dag":
                d[k] = round(d[k], 2)
    vandaag = datetime.now().astimezone().date()
    maand = [d for d in lijst if d["dag"][:7] == vandaag.isoformat()[:7]]
    volle = [d for d in lijst if d["dag"] != vandaag.isoformat()]
    som = lambda xs, k: round(sum(x[k] for x in xs), 2)
    gem = som(volle, "besparing") / len(volle) if volle else 0.0
    return {"dagen": lijst,
            "maand": {"besparing": som(maand, "besparing"), "door_accu": som(maand, "door_accu"),
                      "teruglevering": som(maand, "teruglevering"), "kosten": som(maand, "kosten"), "dagen": len(maand)},
            "per_jaar": round(gem * 365), "volle_dagen": len(volle)}

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


def opbrengst(rijen: list[dict], prijzen, dagen: int = 30) -> dict:
    per_dag: "OrderedDict[str, dict]" = OrderedDict()
    for r in rijen:
        if (r.get("n") or 0) < 3:
            continue
        t = datetime.fromisoformat(r["start"]) if isinstance(r["start"], str) else r["start"]
        dag = t.astimezone().date().isoformat()
        beurs = r.get("beurs")
        if beurs is None:
            beurs = model_prijs(t)
        inkoop, terug = prijzen.inkoop(beurs), prijzen.teruglever(beurs)
        verbruik = (r.get("huis_kwh") or 0) + (r.get("wp_kwh") or 0) + (r.get("auto_kwh") or 0)
        d = per_dag.setdefault(dag, {"dag": dag, "kosten": 0.0, "zonder_accu": 0.0, "zonder_alles": 0.0, "teruglevering": 0.0, "zon_kwh": 0.0})
        d["kosten"] += r.get("kosten") or 0
        d["zonder_accu"] += r.get("kosten_zonder") or 0
        d["zonder_alles"] += verbruik * inkoop
        d["teruglevering"] += (r.get("terug_kwh") or 0) * terug
        d["zon_kwh"] += r.get("zon_kwh") or 0
    lijst = list(per_dag.values())[-dagen:]
    for d in lijst:
        d["besparing"] = d["zonder_alles"] - d["kosten"]
        d["door_accu"] = d["zonder_accu"] - d["kosten"]
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

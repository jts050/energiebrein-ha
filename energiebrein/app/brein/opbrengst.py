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


def kosten_periode(gesch, prijzen, periode: str, accu_kwh: float, eta: float) -> dict:
    """Totalen en per dag (of per maand bij 'jaar') voor het kostenoverzicht."""
    from datetime import date, time, timedelta, timezone
    vandaag = datetime.now().astimezone().date()
    if periode == "vorige":
        eind = vandaag.replace(day=1)
        begin = (eind - timedelta(days=1)).replace(day=1)
    elif periode == "jaar":
        begin, eind = vandaag.replace(month=1, day=1), vandaag + timedelta(days=1)
    else:
        periode, begin, eind = "maand", vandaag.replace(day=1), vandaag + timedelta(days=1)
    van = datetime.combine(begin, time(0)).astimezone() - timedelta(days=1)
    tot = datetime.combine(eind, time(0)).astimezone()
    rijen = gesch.rijen(van, tot)
    o = opbrengst(rijen, prijzen, 400, accu_kwh, eta)
    dagen = [d for d in o["dagen"] if begin.isoformat() <= d["dag"] < eind.isoformat()]
    velden = ("kosten", "zonder_accu", "zonder_alles", "teruglevering", "zon_kwh", "besparing", "door_accu")
    totaal = {k: round(sum(d[k] for d in dagen), 2) for k in velden}
    # energie uit de metingen
    eigen = [r for r in rijen if (r.get("n") or 0) >= 3 and begin.isoformat() <= datetime.fromisoformat(r["start"]).astimezone().date().isoformat() < eind.isoformat()]
    som = lambda k: sum(r.get(k) or 0 for r in eigen)
    totaal.update({"afname_kwh": round(som("afname_kwh"), 1), "terug_kwh": round(som("terug_kwh"), 1),
                   "verbruik_kwh": round(som("huis_kwh") + som("wp_kwh") + som("auto_kwh"), 1),
                   "auto_kwh": round(som("auto_kwh"), 1), "wp_kwh": round(som("wp_kwh"), 1)})
    zon = totaal["zon_kwh"]
    totaal["zelf_gebruikt_pct"] = round(max(zon - totaal["terug_kwh"], 0) / zon * 100) if zon > 1 else None
    totaal["dagen"] = len(dagen)
    maanden = []
    if periode == "jaar":
        per: dict = {}
        for d in dagen:
            m = per.setdefault(d["dag"][:7], {"maand": d["dag"][:7], **{k: 0.0 for k in velden}})
            for k in velden:
                m[k] += d[k]
        maanden = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()} for m in per.values()]
    return {"periode": periode, "van": begin.isoformat(), "tot": eind.isoformat(), "totaal": totaal,
            "dagen": dagen if periode != "jaar" else [], "maanden": maanden}

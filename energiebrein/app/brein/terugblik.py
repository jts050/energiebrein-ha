"""Terugblik: wat had gisteren achteraf het beste gekund?

Met de echte prijzen en het echte verbruik en de echte zon van een dag rekent het brein het beste accuplan uit, alsof het
alles vooraf had geweten. Dat plan begint en eindigt met dezelfde lading als in werkelijkheid, anders is de vergelijking
niet eerlijk. Het verschil met wat er echt gebeurde, zegt hoe goed het brein het deed en waar het misging (meestal een
voorspelling die ernaast zat)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .model import Kwartier
from .planner import Planner

log = logging.getLogger("terugblik")


def _e(x: float) -> str:
    return f"€ {x:.2f}".replace(".", ",")


def _k(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def bereken(cfg: dict, rijen: list[dict], prijs, soc_begin: float | None, ondergrens: float | None,
            voorspeld: list[dict] | None = None) -> dict | None:
    """rijen: de kwartieren van één dag uit de geschiedenis. prijs: object met inkoop(beurs, t) en teruglever(beurs, t)."""
    goed = [r for r in rijen if (r.get("n") or 0) >= 3 and r.get("soc") is not None]
    if len(goed) < 88:                                   # te veel gaten (programma uit): geen eerlijke vergelijking
        return None
    kw = []
    for r in goed:
        t = datetime.fromisoformat(r["start"])
        beurs = r.get("beurs") if r.get("beurs") is not None else 0.1
        ink = r["inkoop"] if r.get("inkoop") is not None else prijs.inkoop(beurs, t)
        ter = r["terug"] if r.get("terug") is not None else prijs.teruglever(beurs, t)
        kw.append(Kwartier(start=t, beurs=beurs, inkoop=ink, teruglever=ter,
                           zon_kwh=max(r.get("zon_kwh") or 0, 0), huis_kwh=max(r.get("huis_kwh") or 0, 0),
                           wp_kwh=max(r.get("wp_kwh") or 0, 0), auto_kwh=max(r.get("auto_kwh") or 0, 0)))
    begin = soc_begin if soc_begin is not None else goed[0]["soc"]
    pl = Planner(cfg)
    pl.ondergrens = ondergrens
    pl.eind_soc = goed[-1]["soc"]
    pl._accu(kw, begin)

    def kost(net, k):
        return net * (k.inkoop if net > 0 else (0.0 if k.aftoppen else k.teruglever))
    beste = sum(kost(k.net_kwh, k) for k in kw)
    echt = sum(r.get("kosten") or 0 for r in goed)
    zonder = sum(r.get("kosten_zonder") or 0 for r in goed)
    # Zit er aan het eind meer (of minder) in de accu dan aan het begin, dan is dat geld waard. Het beste plan eindigt met
    # dezelfde lading als echt, dus het verschil tussen die twee klopt; tegenover "zonder accu" tellen we de lading mee.
    cap = float(cfg["accu"]["capaciteit_kwh"])
    prijzen = sorted(k.inkoop for k in kw)
    waarde = prijzen[len(prijzen) // 2] * 0.9
    lading = (goed[-1]["soc"] - begin) / 100 * cap * waarde
    mogelijk = zonder - (beste - lading)                  # wat de accu maximaal had kunnen besparen
    gehaald = zonder - (echt - lading)
    score = max(0.0, min(1.0, gehaald / mogelijk)) if mogelijk > 0.05 else None

    # Waar ging het mis? Een paar duidelijke patronen, in volgorde van wat het meeste kostte.
    tips = []
    lo = pl.ondergrens_pct()
    leeg = [(k, r) for k, r in zip(kw, goed) if r["soc"] <= lo + 1 and (r.get("net_kwh") or 0) > 0.05 and k.accu_kwh < -0.02]
    if leeg:
        verlies = sum(((r.get("net_kwh") or 0) - max(k.net_kwh, 0)) * k.inkoop for k, r in leeg)
        if verlies > 0.05:
            t0 = datetime.fromisoformat(leeg[0][1]["start"]).astimezone()
            tips.append((verlies, f"De accu was om {t0:%H:%M} al leeg, terwijl stroom daarna nog {_e(max(k.inkoop for k, _ in leeg))} "
                                  f"kostte. Met meer lading op dat moment had je ongeveer {_e(verlies)} bespaard."))
    # uit het net geladen en verkocht: per dag vergelijken (het beste plan mag het op een ander, even goed moment doen)
    def netlading(e_accu, e_net):
        return max(min(e_net, e_accu), 0.0) if e_accu > 0 else 0.0
    echt_net = sum(netlading(r.get("accu_kwh") or 0, r.get("net_kwh") or 0) for r in goed)
    beste_net = sum(netlading(k.accu_kwh, k.net_kwh) for k in kw)
    if echt_net - beste_net > 1:
        prijs_gem = sum(netlading(r.get("accu_kwh") or 0, r.get("net_kwh") or 0) * k.inkoop for k, r in zip(kw, goed)) / echt_net
        tips.append(((echt_net - beste_net) * 0.03, f"De accu laadde {_k(echt_net - beste_net)} kWh meer uit het net dan nodig "
                                                     f"(gemiddeld {_e(prijs_gem)}); achteraf had de zon of minder laden ook gekund."))
    def verkoop(e_accu, e_net):
        return max(-max(e_net, e_accu), 0.0) if e_accu < 0 and e_net < 0 else 0.0
    echt_v = sum(verkoop(r.get("accu_kwh") or 0, r.get("net_kwh") or 0) for r in goed)
    beste_v = sum(verkoop(k.accu_kwh, k.net_kwh) for k in kw)
    if echt_v - beste_v > 1:
        tips.append(((echt_v - beste_v) * 0.03, f"De accu verkocht {_k(echt_v - beste_v)} kWh aan het net die achteraf beter voor later bewaard had kunnen worden."))
    elif beste_v - echt_v > 2:
        tips.append(((beste_v - echt_v) * 0.03, f"Achteraf had de accu {_k(beste_v - echt_v)} kWh meer kunnen terugverkopen op de duurste momenten."))
    # voorspelling: vaak de echte oorzaak
    if voorspeld:
        vm = {v["start"]: v for v in voorspeld}
        paren = [(vm[r["start"]], r) for r in goed if r["start"] in vm]
        if len(paren) > 48:
            for veld, naam in (("huis_kwh", "verbruik"), ("zon_kwh", "zon")):
                e = sum(r.get(veld) or 0 for _, r in paren)
                v = sum(p[veld] or 0 for p, _ in paren)
                if e > 1 and abs(v - e) / e > 0.2:
                    tips.append((abs(v - e) * 0.05, f"Het brein verwachtte {_k(v)} kWh {naam}, het werd {_k(e)} kWh"
                                                    f" ({'meer' if e > v else 'minder'} dan gedacht). Daardoor klopte het plan minder."))
    tips.sort(key=lambda x: -x[0])
    if echt - beste < 0.10:
        tips = []                                         # bijna perfect: niets te melden
    dag = datetime.fromisoformat(goed[0]["start"]).astimezone().date()
    return {"dag": dag.isoformat(), "echt": round(echt, 2), "beste": round(beste, 2), "zonder": round(zonder, 2),
            "verschil": round(echt - beste, 2), "score": round(score, 2) if score is not None else None,
            "tips": [t for _, t in tips[:3]]}


class Terugblik:
    def __init__(self, data: Path):
        self.pad = data / "terugblik.json"
        self.dagen: dict[str, dict] = {}
        try:
            self.dagen = json.loads(self.pad.read_text())
        except Exception:
            pass

    def doe(self, brein) -> None:
        """Rekent gisteren (en zo nodig een paar dagen daarvoor) uit, als dat nog niet gebeurd is."""
        vandaag = datetime.now().astimezone().date()
        for terug in range(1, 4):
            dag = vandaag - timedelta(days=terug)
            if dag.isoformat() in self.dagen:
                continue
            van = datetime.combine(dag, time(0)).astimezone()
            tot = van + timedelta(days=1)
            try:
                r = bereken(brein.cfg, brein.gesch.rijen(van, tot), brein.prijzen, brein.gesch.soc_voor(van),
                            brein._omvormer_grens(), brein.gesch.voorspellingen(van, tot))
            except Exception:
                log.exception("Terugblik voor %s mislukt", dag)
                continue
            self.dagen[dag.isoformat()] = r or {"dag": dag.isoformat(), "onvolledig": True}
        self.dagen = dict(sorted(self.dagen.items())[-90:])
        try:
            self.pad.write_text(json.dumps(self.dagen))
        except Exception as e:
            log.warning("Terugblik bewaren mislukt: %s", e)

    def status(self) -> dict:
        volledig = [d for d in self.dagen.values() if not d.get("onvolledig")]
        laatste = volledig[-1] if volledig else None
        week = [d["score"] for d in volledig[-7:] if d.get("score") is not None]
        return {"laatste": laatste, "score_week": round(sum(week) / len(week), 2) if week else None, "dagen_week": len(week)}

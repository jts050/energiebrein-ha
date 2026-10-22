"""Welke leverancier was voor jou het goedkoopst? Gerekend met je eigen gemeten afname en teruglevering per kwartier."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .prijzen import model_prijs

BESTAND = Path(__file__).resolve().parent / "leveranciers.yaml"


def lijst() -> dict:
    return yaml.safe_load(BESTAND.read_text(encoding="utf-8"))


def vergelijk(rijen: list[dict], prijzen_cfg: dict) -> dict:
    data = lijst()
    rijen = [r for r in rijen if (r.get("n") or 0) >= 3]
    if not rijen:
        return {"dagen": 0, "rijen": [], "peildatum": data["peildatum"]}
    btw, eb = prijzen_cfg["btw"], prijzen_cfg["energiebelasting"]
    dagen = len(rijen) / 96
    zonder_beurs = 0
    uit = []
    for sleutel, l in data["leveranciers"].items():
        stroom = 0.0
        for r in rijen:
            beurs = r.get("beurs")
            if beurs is None:
                beurs = model_prijs(datetime.fromisoformat(r["start"]))
            af = r.get("afname_kwh")
            terug = r.get("terug_kwh")
            if af is None:   # oude rij: alleen netto bekend
                af, terug = max(r["net_kwh"], 0), max(-r["net_kwh"], 0)
            stroom += af * ((beurs + l["opslag"]) * (1 + btw) + eb) - terug * (beurs - l["terugleverkosten"])
        vast = l["vast"] * 12 / 365 * dagen
        per_maand = (stroom + vast) / dagen * 30.44
        uit.append({"id": sleutel, "naam": l["naam"], "per_maand": round(per_maand, 2), "per_jaar": round(per_maand * 12),
                    "handel": l.get("handel", ""), "opslag": l["opslag"], "vast": l["vast"], "terugleverkosten": l["terugleverkosten"]})
    zonder_beurs = sum(1 for r in rijen if r.get("beurs") is None)
    uit.sort(key=lambda x: x["per_maand"])
    huidig = prijzen_cfg.get("leverancier")
    ref = next((x for x in uit if x["id"] == huidig), None)
    for x in uit:
        x["verschil_jaar"] = round(x["per_jaar"] - ref["per_jaar"]) if ref else None
    return {"dagen": round(dagen, 1), "rijen": uit, "huidig": huidig, "peildatum": data["peildatum"],
            "geschatte_prijzen_pct": round(zonder_beurs / len(rijen) * 100)}

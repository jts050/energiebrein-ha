"""Slim laden van de auto: een weekschema met vertrektijden, een doel in km of kWh, en een eenmalige uitzondering.
Wordt bewaard in de datamap, zodat het een herstart overleeft."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("autoplan")
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
STANDAARD = {
    "week": {"ma": "07:30", "di": "07:30", "wo": "07:30", "do": "07:30", "vr": "07:30", "za": None, "zo": None},
    "doel_soort": "km",            # km of kwh
    "doel_km": 80,
    "doel_kwh": 15,
    "doel_pct": 80,                # alleen met een gekoppelde auto (laadstand bekend)
    "verbruik_kwh_100km": 18,
    "eenmalig": None,              # {"vertrek": iso-tijd, "doel_soort": "km"|"kwh", "doel": getal}
    "zonder_vertrek": "zon",       # wat te doen als er geen vertrek gepland is: zon (alleen op overschot) of goedkoop
}


def _tijd(s: str | None):
    if not s:
        return None
    h, m = (int(x) for x in s.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError
    return h, m


class AutoPlan:
    def __init__(self, pad: Path):
        self.pad = pad
        self.d = json.loads(json.dumps(STANDAARD))
        try:
            opgeslagen = json.loads(pad.read_text(encoding="utf-8"))
            self.d.update({k: v for k, v in opgeslagen.items() if k in STANDAARD})
            self.d["week"] = {**STANDAARD["week"], **(opgeslagen.get("week") or {})}
        except Exception:
            pass

    def bewaar(self):
        try:
            self.pad.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.warning("Autoplan opslaan mislukt: %s", e)

    def zet(self, nieuw: dict) -> None:
        """Controleert en neemt over wat de app stuurt."""
        week = nieuw.get("week", self.d["week"])
        for dag in DAGEN:
            _tijd(week.get(dag))                                  # gooit ValueError bij onzin
        soort = nieuw.get("doel_soort", self.d["doel_soort"])
        if soort not in ("km", "kwh", "pct"):
            raise ValueError("doel_soort moet km, kwh of pct zijn")
        pct = float(nieuw.get("doel_pct", self.d.get("doel_pct", 80)))
        if not 20 <= pct <= 100:
            raise ValueError("Laden tot moet tussen 20 en 100% liggen")
        km, kwh = float(nieuw.get("doel_km", self.d["doel_km"])), float(nieuw.get("doel_kwh", self.d["doel_kwh"]))
        verbruik = float(nieuw.get("verbruik_kwh_100km", self.d["verbruik_kwh_100km"]))
        if not (0 <= km <= 1000 and 0 <= kwh <= 200 and 8 <= verbruik <= 40):
            raise ValueError("Doel of verbruik buiten bereik")
        zonder = nieuw.get("zonder_vertrek", self.d["zonder_vertrek"])
        if zonder not in ("zon", "goedkoop"):
            raise ValueError("zonder_vertrek moet zon of goedkoop zijn")
        self.d.update({"week": {d: week.get(d) or None for d in DAGEN}, "doel_soort": soort, "doel_km": km, "doel_kwh": kwh, "doel_pct": pct,
                       "verbruik_kwh_100km": verbruik, "zonder_vertrek": zonder})
        if "eenmalig" in nieuw:
            e = nieuw["eenmalig"]
            if e:
                datetime.fromisoformat(e["vertrek"])
                if e.get("doel_soort") not in ("km", "kwh", "pct") or not 0 <= float(e.get("doel", 0)) <= 1000:
                    raise ValueError("Eenmalig doel klopt niet")
            self.d["eenmalig"] = e or None
        self.bewaar()

    # ---------------------------------------------------------------- uitkomst voor de planner
    def _kwh(self, soort: str, waarde: float) -> float:
        if soort == "pct":                     # wordt in het brein omgerekend met de laadstand van de auto
            return 0.0
        return waarde * self.d["verbruik_kwh_100km"] / 100 if soort == "km" else waarde

    def doel_pct(self, nu: datetime) -> float | None:
        """Laden tot dit percentage (alleen als het actieve doel in % is)."""
        e = self.eenmalig_actief(nu)
        if e:
            return float(e["doel"]) if e["doel_soort"] == "pct" else None
        return float(self.d.get("doel_pct", 80)) if self.d["doel_soort"] == "pct" else None

    def eenmalig_actief(self, nu: datetime) -> dict | None:
        e = self.d.get("eenmalig")
        if not e:
            return None
        t = datetime.fromisoformat(e["vertrek"])
        if t.tzinfo is None:
            t = t.astimezone()
        if t <= nu:
            return None
        return {**e, "tijd": t.astimezone(timezone.utc)}

    def volgende(self, nu: datetime) -> tuple[datetime | None, float, str]:
        """(vertrektijd in UTC of None, benodigde kWh, korte uitleg)"""
        e = self.eenmalig_actief(nu)
        if e:
            return e["tijd"], self._kwh(e["doel_soort"], float(e["doel"])), "eenmalig"
        lokaal = nu.astimezone()
        for i in range(8):
            dag = lokaal.date() + timedelta(days=i)
            t = _tijd(self.d["week"].get(DAGEN[dag.weekday()]))
            if not t:
                continue
            # via een 'kale' tijd, zodat zomer- en wintertijd per dag goed gaan
            vertrek = datetime(dag.year, dag.month, dag.day, t[0], t[1]).astimezone()
            if vertrek > lokaal:
                return vertrek.astimezone(timezone.utc), self._kwh(self.d["doel_soort"], float(self.d.get("doel_" + self.d["doel_soort"], 0))), "schema"
        return None, self._kwh(self.d["doel_soort"], float(self.d.get("doel_" + self.d["doel_soort"], 0))), "geen"

    def dict(self) -> dict:
        return dict(self.d)

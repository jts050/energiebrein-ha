"""Conditie van de thuisaccu.

Twee bronnen:
- wat de accu (de BMS) zelf meldt via de omvormer: gezondheid (SOH), temperatuur, aantal modules, fouten;
- wat Energiebrein zelf meet uit de geschiedenis: aantal cycli, hoeveel kWh er echt in de accu past, en het rendement.

De BMS-gezondheid zegt vaak jarenlang 100% en springt dan in grote stappen omlaag. De gemeten capaciteit is eerlijker:
die volgt uit stukken waarin de accu flink laadt of ontlaadt (minstens 25 procentpunt), als kWh gedeeld door de
verandering in lading."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

log = logging.getLogger("conditie")

MIN_STUK = 25.0          # procentpunt: kortere stukken zijn te onnauwkeurig (de lading wordt in hele procenten gemeld)


def stukken(rijen: list[dict]) -> list[dict]:
    """Aaneengesloten stukken laden of ontladen, met de kWh aan de accukant en de verandering in lading."""
    uit, cur = [], None
    vorige_soc = None
    for r in rijen:
        e, soc = r.get("accu_kwh") or 0.0, r.get("soc")
        if soc is None or (r.get("n") or 0) < 3:
            if cur:
                uit.append(cur)
            cur, vorige_soc = None, soc
            continue
        richting = 1 if e > 0.03 else -1 if e < -0.03 else 0
        if cur and (richting not in (0, cur["richting"]) or cur["stil"] > 2):
            uit.append(cur)
            cur = None
        if cur is None and richting != 0 and vorige_soc is not None:
            cur = {"richting": richting, "soc_begin": vorige_soc, "kwh": 0.0, "stil": 0, "start": r["start"]}
        if cur:
            cur["kwh"] += abs(e) if richting == cur["richting"] else 0.0
            cur["stil"] = cur["stil"] + 1 if richting == 0 else 0
            cur["soc_eind"] = soc
            cur["eind"] = r["start"]
        vorige_soc = soc
    if cur:
        uit.append(cur)
    return [s for s in uit if "soc_eind" in s]


def capaciteit(rijen: list[dict]) -> tuple[float | None, int]:
    """Gemeten bruikbare capaciteit (kWh) uit de langere laad- en ontlaadstukken, en op hoeveel stukken dat rust.
    Laden geeft een iets te hoge waarde (verlies in de accu), ontladen een iets te lage; daarom het midden van beide."""
    laad, ontl = [], []
    for s in stukken(rijen):
        d = (s["soc_eind"] - s["soc_begin"]) * s["richting"]
        if d >= MIN_STUK and s["kwh"] > 0.5:
            (laad if s["richting"] > 0 else ontl).append(s["kwh"] / (d / 100))
    if not laad and not ontl:
        return None, 0
    if laad and ontl:
        return (median(laad) + median(ontl)) / 2, len(laad) + len(ontl)
    return median(laad or ontl), len(laad) + len(ontl)


def rendement(rijen: list[dict], cap: float) -> float | None:
    """Wat er uit de accu kwam tegenover wat erin ging, gecorrigeerd voor wat er aan het eind meer of minder in zit."""
    goed = [r for r in rijen if r.get("soc") is not None and (r.get("n") or 0) >= 3]
    if len(goed) < 96:
        return None
    erin = sum(max(r.get("accu_kwh") or 0, 0) for r in goed)
    eruit = -sum(min(r.get("accu_kwh") or 0, 0) for r in goed)
    if erin < 2 * cap:                   # pas zinvol na een paar keer flink laden
        return None
    verschil = (goed[-1]["soc"] - goed[0]["soc"]) / 100 * cap
    r = (eruit + verschil) / erin
    return r if 0.6 < r < 1.05 else None


class Conditie:
    def __init__(self, cfg: dict, gesch, data: Path):
        self.cfg, self.gesch = cfg, gesch
        self.pad = data / "conditie.json"
        self.maanden: dict[str, dict] = {}       # "2026-10" → {"soh", "capaciteit", "rendement", "cycli"}
        self.max_modules: int | None = None
        self.laatst: dict = {}
        try:
            z = json.loads(self.pad.read_text())
            self.maanden, self.max_modules = z.get("maanden", {}), z.get("max_modules")
        except Exception:
            pass

    def bereken(self, bms: dict | None) -> dict:
        """Elk uur opnieuw uitrekenen (goedkoop: alleen de geschiedenis van de laatste 60 dagen en het totaal)."""
        nu = datetime.now(timezone.utc)
        cap_ingesteld = float(self.cfg["accu"]["capaciteit_kwh"])
        alles = self.gesch.rijen(datetime(2020, 1, 1, tzinfo=timezone.utc), nu)
        geleverd = -sum(min(r.get("accu_kwh") or 0, 0) for r in alles)
        recent = [r for r in alles if r["start"] >= (nu - timedelta(days=60)).isoformat()]
        cap, n = capaciteit(recent)
        rend = rendement([r for r in alles if r["start"] >= (nu - timedelta(days=30)).isoformat()], cap or cap_ingesteld)
        bms = bms or {}
        modules = bms.get("modules")
        if modules:
            self.max_modules = max(self.max_modules or 0, int(modules))
        uit = {
            "soh": bms.get("soh"), "temperatuur": bms.get("temperatuur"), "modules": modules, "max_modules": self.max_modules,
            "fout": bms.get("fout"), "laden_a": bms.get("laden_a"), "ontladen_a": bms.get("ontladen_a"),
            "cycli": round(geleverd / cap_ingesteld, 1) if cap_ingesteld else None,
            "capaciteit": round(cap, 1) if cap else None, "capaciteit_stukken": n, "capaciteit_ingesteld": cap_ingesteld,
            "rendement": round(rend, 3) if rend else None,
            "sinds": alles[0]["start"] if alles else None,
        }
        maand = nu.astimezone().strftime("%Y-%m")
        self.maanden[maand] = {k: uit[k] for k in ("soh", "capaciteit", "rendement", "cycli")}
        uit["maanden"] = dict(sorted(self.maanden.items())[-24:])
        self.laatst = uit
        try:
            self.pad.write_text(json.dumps({"maanden": self.maanden, "max_modules": self.max_modules}))
        except Exception as e:
            log.warning("Conditie bewaren mislukt: %s", e)
        return uit

    def storingen(self) -> list[dict]:
        """Meldingen als de accu achteruitgaat of de BMS iets meldt."""
        c, uit = self.laatst, []
        if not c:
            return uit
        if c.get("modules") and c.get("max_modules") and c["modules"] < c["max_modules"]:
            uit.append({"ernst": "fout", "apparaat": "omvormer", "titel": "Een accumodule doet niet meer mee",
                        "uitleg": f"De accu meldt {c['modules']} modules, eerder waren het er {c['max_modules']}. "
                                  "Er is dan minder capaciteit en minder vermogen.",
                        "actie": "Kijk of alle modules aan staan en de kabels tussen de modules goed vastzitten. Helpt dat niet, neem dan contact op met de leverancier."})
        if c.get("soh") is not None and c["soh"] < 80:
            uit.append({"ernst": "let op", "apparaat": "omvormer", "titel": f"De accu meldt een gezondheid van {c['soh']}%",
                        "uitleg": "Onder de 80% is de accu merkbaar versleten. Kijk wat de garantie zegt.",
                        "actie": "Vraag de leverancier of de accu onder de garantie valt."})
        if c.get("capaciteit") and c.get("capaciteit_stukken", 0) >= 6 and c["capaciteit"] < 0.8 * c["capaciteit_ingesteld"]:
            pct = round(c["capaciteit"] / c["capaciteit_ingesteld"] * 100)
            uit.append({"ernst": "let op", "apparaat": "omvormer", "titel": f"Er past nog maar {pct}% van de capaciteit in de accu",
                        "uitleg": f"Gemeten: ongeveer {c['capaciteit']:.1f} kWh tussen leeg en vol, ingesteld is {c['capaciteit_ingesteld']:.0f} kWh.".replace(".", ",", 1)
                                  + " Klopt de ingestelde capaciteit, dan is de accu achteruitgegaan.",
                        "actie": "Kijk bij Koppelen of de capaciteit goed is ingesteld. Zo ja: vraag de leverancier naar de garantie."})
        t = c.get("temperatuur")
        if t is not None and (t >= 45 or t <= 0):
            uit.append({"ernst": "let op", "apparaat": "omvormer", "titel": f"De accu is {'erg warm' if t >= 45 else 'erg koud'} ({t} °C)",
                        "uitleg": "Bij deze temperatuur laadt en levert de accu minder, en slijt hij sneller.",
                        "actie": "Zorg voor ventilatie op de zolder" if t >= 45 else "Bij vorst laadt een lithiumaccu niet; dat is normaal en gaat vanzelf weer goed als het warmer wordt."})
        return uit

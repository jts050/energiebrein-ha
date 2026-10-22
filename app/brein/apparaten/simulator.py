"""Een nagebootst huis: zon, verbruik, accu, auto en warmtepomp. Zo kun je alles proberen zonder echte apparaten.
De simulator gedraagt zich zoals de echte drivers: hij krijgt dezelfde opdrachten en geeft dezelfde metingen terug."""
from __future__ import annotations
import math
import random
from datetime import datetime, timedelta, timezone

from ..model import Meting
from ..voorspelling import zon_model_kwh, DAGPROFIEL, MAAND
from ..weer import normaal


def huishouden_kw(t: datetime, jaar_kwh: float, r: random.Random, temp: float | None = None) -> float:
    """Een nagebootst gezin met vaste gewoontes, zodat de AI iets te leren heeft:
    doordeweeks vroeg op en douchen (elektrische boiler), weekend later; wasmachine di/do/za,
    vaatwasser na het eten, en meer verbruik als het koud is (verlichting, kachel)."""
    lokaal = t.astimezone()
    h = lokaal.hour + lokaal.minute / 60
    wd = lokaal.weekday()
    temp = normaal(t) if temp is None else temp
    basis = jaar_kwh / 8760 * DAGPROFIEL[lokaal.hour] / (sum(DAGPROFIEL) / 24) * MAAND[lokaal.month - 1] * 0.75
    weekend = wd >= 5
    ochtend = (8.5, 10.0) if weekend else (6.75, 7.75)
    if ochtend[0] <= h < ochtend[1]:
        basis += 1.2
    if wd in (1, 3) and 19.0 <= h < 20.5 or wd == 5 and 10.0 <= h < 11.5:
        basis += 0.9                       # wasmachine
    if 20.5 <= h < 21.75:
        basis += 0.6                       # vaatwasser
    if 17.25 <= h < 18.25:
        basis += 1.0                       # koken
    if 16 <= h or h < 1:
        basis += max(0.0, 12 - temp) * 0.025   # koud: meer licht en elektrische bijverwarming
    return max(0.05, basis * r.uniform(0.85, 1.15))


class SimHuis:
    naam = "Simulatie"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        a = cfg["accu"]
        self.cap = a["capaciteit_kwh"]
        self.soc = 45.0
        self.eta = math.sqrt(a["rendement"])
        self.accu_opdracht = ("zelfverbruik", 0.0, 100.0)
        self.export_limiet: float | None = None
        self.auto_a = 0.0
        self.auto_geladen = 0.0
        self.wp_stand = "normaal"
        self.wolk = 0.8
        self.laatst: datetime | None = None
        self.kwp = sum(v["kwp"] for v in cfg.get("zonnepanelen", []))
        self.r = random.Random(7)
        # meteen een realistisch beginpunt
        nu = datetime.now().astimezone()
        self.auto_verbonden = nu.hour >= 17 or nu.hour < 8
        self.auto_geladen = 4.0 if self.auto_verbonden else 0.0

    async def start(self):
        return

    async def stop(self):
        return

    # --- omvormer + accu ---
    async def accu(self, actie: str, kw: float = 0.0, doel_soc: float = 100.0):
        self.accu_opdracht = (actie, kw, doel_soc)

    async def exportlimiet(self, kw: float | None):
        self.export_limiet = kw

    # --- laadpaal ---
    async def laadstroom(self, ampere: float):
        self.auto_a = ampere

    # --- warmtepomp ---
    async def warmtepomp(self, stand: str):
        self.wp_stand = stand

    async def lees(self) -> Meting:
        nu = datetime.now(timezone.utc)
        dt = (nu - self.laatst).total_seconds() / 3600 if self.laatst else 0.0
        self.laatst = nu
        lokaal = nu.astimezone()
        h = lokaal.hour + lokaal.minute / 60

        # auto komt om 17:30 thuis en vertrekt om 07:30
        thuis = h >= 17.5 or h < 7.5
        if thuis and not self.auto_verbonden:
            self.auto_verbonden, self.auto_geladen = True, 0.0
        elif not thuis and self.auto_verbonden:
            self.auto_verbonden, self.auto_a = False, 0.0

        self.wolk = min(1.3, max(0.15, self.wolk + self.r.uniform(-0.03, 0.03)))
        zon = zon_model_kwh(nu, self.kwp) * 4 * self.wolk
        huis = huishouden_kw(nu, self.cfg["huis"]["jaarverbruik_kwh"], self.r)
        if self.r.random() < 0.01:
            huis += self.r.choice([1.8, 2.2])  # waterkoker, oven

        wpc = self.cfg.get("warmtepomp", {})
        wp = 0.0
        if wpc.get("type", "uit") != "uit":
            basis = min(wpc["vermogen_kw"], max(0.05, (16 - normaal(nu)) * 0.09))
            wp = {"boost": wpc["vermogen_kw"], "wacht": 0.0}.get(self.wp_stand, basis)

        lp = self.cfg.get("laadpaal", {})
        auto = 0.0
        if self.auto_verbonden and self.auto_a >= lp.get("min_stroom_a", 6) and self.auto_geladen < lp.get("auto_accu_kwh", 58) * 0.8:
            auto = self.auto_a * 230 * lp.get("fasen", 3) / 1000
            self.auto_geladen += auto * dt

        # accu volgt de opdracht, binnen grenzen
        a = self.cfg["accu"]
        actie, kw, doel = self.accu_opdracht
        rest = huis + wp + auto - zon
        if actie == "laden":
            p = min(kw, a["max_laden_kw"]) if self.soc < doel else 0.0
        elif actie == "ontladen":
            p = -min(kw, a["max_ontladen_kw"])
        elif actie == "vasthouden":
            p = 0.0
        else:  # zelfverbruik: overschot opslaan, tekort aanvullen
            p = min(-rest, a["max_laden_kw"]) if rest < 0 else -min(rest, a["max_ontladen_kw"])
        if p > 0 and self.soc >= a["max_soc"]:
            p = 0.0
        if p < 0 and self.soc <= a["min_soc"]:
            p = 0.0
        self.soc += (p * self.eta if p > 0 else p / self.eta) * dt / self.cap * 100
        self.soc = min(100.0, max(0.0, self.soc))

        net = rest + p
        if self.export_limiet is not None and net < -self.export_limiet:
            zon += net + self.export_limiet   # omvormer knijpt de panelen af
            net = -self.export_limiet
        return Meting(tijd=nu, net_kw=net, zon_kw=max(zon, 0), accu_kw=p, accu_soc=self.soc,
                      auto_kw=auto, auto_verbonden=self.auto_verbonden, auto_geladen_kwh=self.auto_geladen,
                      wp_kw=wp, wp_stand=self.wp_stand, huis_kw=huis)

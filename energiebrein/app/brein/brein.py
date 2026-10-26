"""Het brein: haalt prijzen en voorspellingen op, maakt elk kwartier een plan en stuurt elke paar seconden bij."""
from __future__ import annotations
import asyncio
import time
import logging
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .apparaten import Huis
from .config import data_map
from .geschiedenis import Geschiedenis
from .model import Bediening, Kwartier, Meting
from .planner import zon_in_accu, Planner, AutoStatus, mag_verkopen
from .prijzen import Prijzen, kwartier_start
from .voorspelling import Voorspelling

log = logging.getLogger("brein")


class Brein:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.huis = Huis(cfg)
        self.gesch = Geschiedenis(data_map(cfg) / "geschiedenis.db")
        self.prijzen = Prijzen(cfg)
        self.voorsp = Voorspelling(cfg, self.gesch)
        from .conditie import Conditie
        self.conditie = Conditie(cfg, self.gesch, data_map(cfg))
        from .terugblik import Terugblik
        self.terugblik = Terugblik(data_map(cfg))
        self.planner = Planner(cfg)
        self.planner.temp = self.voorsp.weer.op if hasattr(self.voorsp, "weer") else None
        from .licentie import Licentie
        self.licentie = Licentie(cfg)
        from .logboek import Logboek
        self.logboek = Logboek(data_map(cfg) / "logboek.db")
        from .klemtest import KlemTest
        self.klemtest = KlemTest(self)
        self.huis.logboek = self.logboek
        self._storingen_vorig: dict | None = None
        from .telefoon import Telefoon
        self.telefoon = Telefoon(cfg)
        lp = cfg.get("laadpaal", {})
        self.bed = Bediening(auto_doel_kwh=lp.get("standaard_doel_kwh", 20), auto_vertrek=lp.get("standaard_vertrek", "07:30"))
        from .autoplan import AutoPlan
        self.autoplan = AutoPlan(data_map(cfg) / "auto.json")
        self._bed_pad = data_map(cfg) / "bediening.json"
        try:   # je keuzes in Bediening overleven een herstart
            import json as _j
            for k, v in _j.loads(self._bed_pad.read_text()).items():
                if k in ("accu", "laadpaal", "warmtepomp", "zon"):
                    setattr(self.bed, k, v)
        except Exception:
            pass
        from .apparaten.autos import AutoAccount
        self.autoacc = AutoAccount(cfg, data_map(cfg))
        self._auto_pct_doel: float | None = None
        self._auto_periode = None      # vertrektijd waarvoor we nu laden
        self._auto_basis = 0.0         # kWh die al in de sessie zat bij het begin van deze periode
        self.plan: dict = {"kwartieren": [], "samenvatting": {}}
        self.meting: Meting | None = None
        self.besluit: dict = {}
        self._taken: list[asyncio.Task] = []
        self._plan_nodig = asyncio.Event()
        self._laatst_gemeten: datetime | None = None
        self._meter_reeks: deque = deque()      # (tijd, slimme meter kW, CT-meter omvormer kW) van de laatste 10 minuten

    # ------------------------------------------------------------ levenscyclus
    async def start(self):
        await self.huis.start()
        self.logboek.schrijf("systeem", "Energiebrein gestart" + (" in de simulatie" if self.huis.sim else " voor je eigen huis")
                             + ". Gekoppeld: " + (", ".join(f"{k} {v}" for k, v in self.huis.onderdelen().items() if v) or "niets"))
        try:
            await asyncio.wait_for(self.licentie.controleer(self.huis.onderdelen()), timeout=25)
        except Exception:
            pass
        await self.pas_licentie_toe()
        await self.prijzen.ververs()
        await self.voorsp.ververs_zon()
        await self.voorsp.weer.ververs()
        self.voorsp.leer(train=True)
        self.meting = await self.huis.lees()
        self._bereken_conditie()
        self.maak_plan()
        self._taken = [asyncio.create_task(self._regel_lus()), asyncio.create_task(self._plan_lus()),
                       asyncio.create_task(self._data_lus()), asyncio.create_task(self._licentie_lus()),
                       asyncio.create_task(self._auto_lus())]
        from .telefoon import luister
        self._taken.append(asyncio.create_task(luister(self.knop_op_melding)))
        self._gestart = True

    async def pas_licentie_toe(self):
        """Gratis versie: apparaten eenmalig terug naar normaal en daarna alleen nog uitlezen en adviseren."""
        slot = not self.licentie.plus and not self.huis.sim
        if slot and not self.huis.vergrendeld:
            log.info("Geen Plus: apparaten terug naar normaal, verder alleen advies")
            await self.huis.normaal()
        self.huis.vergrendeld = slot

    async def _licentie_lus(self):
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await self.licentie.controleer(self.huis.onderdelen())
                await self.pas_licentie_toe()
            except Exception:
                log.exception("Licentie controleren mislukt")

    async def stop(self):
        for t in self._taken:
            t.cancel()
        self.autoacc.stop()
        await self.huis.stop()
        self.logboek.schrijf("systeem", "Energiebrein gestopt; apparaten terug naar de gewone stand")

    # ------------------------------------------------------------ plannen
    def maak_plan(self):
        nu = datetime.now(timezone.utc)
        prijzen = self.prijzen.vanaf(nu)[: 4 * 48]
        self.voorsp.bereid([t for t, _ in prijzen])
        kw = []
        for t, beurs in prijzen:
            k = Kwartier(start=t, beurs=beurs, inkoop=self.prijzen.inkoop(beurs, t), teruglever=self.prijzen.teruglever(beurs, t),
                         zon_kwh=self.voorsp.zon_kwh(t), huis_kwh=self.voorsp.huis_kwh(t))
            self.voorsp.bewaar(t, k.zon_kwh)
            kw.append(k)
        # De voorspelling van minstens een uur van tevoren bewaren, om later te vergelijken met wat het huis echt deed.
        # (Een voorspelling voor het lopende kwartier is geen kunst meer.)
        try:
            self.gesch.bewaar_voorspelling([(k.start, k.huis_kwh, k.zon_kwh) for k in kw if k.start - nu >= timedelta(hours=1)])
        except Exception:
            log.exception("Voorspelling bewaren mislukt")
        if kw:  # het lopende kwartier is al deels voorbij
            deel = 1 - (nu - kw[0].start).total_seconds() / 900
            kw[0].zon_kwh *= deel
            kw[0].huis_kwh *= deel
        m = self.meting
        self._zet_autodoel(nu)
        auto = AutoStatus(verbonden=bool(m and m.auto_verbonden), geladen_kwh=self.auto_geladen(m))
        self.planner.ondergrens = self._omvormer_grens()
        self.planner.storm = self.stormkwartieren(kw)
        self.plan = self.planner.maak(kw, m.accu_soc if m else 50.0, self.bed, auto, self.voorsp.wp_dag_kwh)
        if getattr(self, "_auto_net_aan", False):
            self._auto_net_aan = False
            try:
                self._meld_auto_aangesloten()
            except Exception:
                log.exception("Melding auto aangesloten mislukt")
        log.info("Nieuw plan: %d kwartieren, verwachte besparing %s", len(kw), self.plan["samenvatting"].get("besparing"))
        try:
            self._meld_bijzondere_prijzen(kw)
        except Exception:
            log.exception("Melding over bijzondere prijzen mislukt")

    def stormkwartieren(self, kw: list) -> dict:
        """Met de stormstand aan: de kwartieren vanaf 2 uur vóór tot het eind van onweer of zware windstoten."""
        self.storm_info = None
        if not self.cfg["accu"].get("stormstand"):
            return {}
        storm = self.voorsp.weer.storm
        uit = {}
        for k in kw:
            for u, reden in storm.items():
                if u - timedelta(hours=2) <= k.start < u + timedelta(hours=1):
                    uit[k.start] = reden
                    break
        if uit:
            eerste = min(uit)
            self.storm_info = {"reden": uit[eerste], "van": eerste.isoformat(),
                               "tot": (max(uit) + timedelta(minutes=15)).isoformat()}
            sleutel = f"storm-{eerste.astimezone():%Y-%m-%d}"
            if sleutel not in getattr(self, "_prijs_gemeld", set()):
                self._prijs_gemeld = getattr(self, "_prijs_gemeld", set()) | {sleutel}
                tekst = (f"{uit[eerste].capitalize()} verwacht rond {eerste.astimezone():%H:%M}. De accu wordt vooraf vol geladen en "
                         "vol gehouden, voor als de stroom uitvalt.")
                self.logboek.schrijf("besluit", tekst, "omvormer")
                self.telefoon.meld(sleutel, "Energiebrein: stormstand", tekst)
        return uit

    @staticmethod
    def prijsvensters(kw: list, nu: datetime) -> list[dict]:
        """Aaneengesloten stukken in de komende 24 uur waarin je geld krijgt voor stroom (negatieve inkoopprijs), of waarin
        terugleveren geld kost (negatieve terugleverprijs)."""
        uit, cur = [], None
        for k in kw:
            if k.start > nu + timedelta(hours=24):
                break
            soort = "betaald" if k.inkoop < 0 else "terug_kost" if k.teruglever < 0 else None
            if cur and (soort != cur["soort"] or k.start != cur["tot"]):
                uit.append(cur)
                cur = None
            if soort:
                if not cur:
                    cur = {"soort": soort, "van": k.start, "tot": k.start, "uiterste": 0.0}
                cur["tot"] = k.start + timedelta(minutes=15)
                cur["uiterste"] = max(cur["uiterste"], -(k.inkoop if soort == "betaald" else k.teruglever))
        if cur:
            uit.append(cur)
        return [v for v in uit if v["tot"] - v["van"] >= timedelta(minutes=30)]

    def _meld_bijzondere_prijzen(self, kw: list):
        """Eén keer per venster een melding op je telefoon (en in het logboek), zodat je wasmachine en vaatwasser kunt inplannen."""
        nu = datetime.now(timezone.utc)
        gemeld = getattr(self, "_prijs_gemeld", set())
        self._prijs_gemeld = gemeld
        for v in self.prijsvensters(kw, nu):
            sleutel = f"{v['soort']}-{v['van'].isoformat()}"
            if sleutel in gemeld:
                continue
            gemeld.add(sleutel)
            van, tot = v["van"].astimezone(), v["tot"].astimezone()
            dag = "vandaag" if van.date() == nu.astimezone().date() else "morgen"
            tijd = f"{dag} {van:%H:%M}–{tot:%H:%M}"
            if v["soort"] == "betaald":
                titel = "Straks krijg je geld voor stroom"
                tekst = (f"{tijd.capitalize()} krijg je tot {self._e(v['uiterste'])} per kWh toe als je stroom gebruikt. De accu laadt dan "
                         "vol uit het net. Zet in die tijd ook de vaatwasser, wasmachine of droger aan, of laad de auto.")
            else:
                titel = "Straks kost terugleveren geld"
                tekst = (f"{tijd.capitalize()} kost terugleveren tot {self._e(v['uiterste'])} per kWh. De zon gaat eerst de accu in"
                         + (" en de panelen worden zo nodig afgetopt" if self.cfg["omvormer"].get("aftoppen_bij_negatieve_prijs", True) else "")
                         + ". Gebruik in die tijd zelf zoveel mogelijk stroom: wasmachine, vaatwasser, auto laden.")
            self.logboek.schrijf("besluit", f"{titel}: {tekst}", "contract")
            self.telefoon.meld(sleutel, f"Energiebrein: {titel}", tekst)
        if len(gemeld) > 50:
            self._prijs_gemeld = set(sorted(gemeld)[-20:])

    def _omvormer_grens(self) -> float | None:
        """Ondergrens die de omvormer zelf hanteert (bij GoodWe de ontlaaddiepte), als hij die meldt."""
        g = (getattr(self.huis.omvormer, "stand", None) or {}).get("ondergrens_pct")
        return float(g) if g is not None else None

    def ondergrens(self) -> float:
        """Tot hoever de accu echt leeg kan: de hoogste van de grens in Energiebrein en die in de omvormer."""
        return max(float(self.cfg["accu"]["min_soc"]), self._omvormer_grens() or 0.0)

    def bewaar_bediening(self):
        import json as _j
        try:
            self._bed_pad.write_text(_j.dumps({k: getattr(self.bed, k) for k in ("accu", "laadpaal", "warmtepomp", "zon")}))
        except Exception as e:
            log.warning("Bediening opslaan mislukt: %s", e)

    def auto_geladen(self, m: Meting | None = None) -> float:
        """Wat er sinds het begin van deze laadperiode (sinds het vorige vertrek) in de auto is gegaan."""
        m = m or self.meting
        if not m or self._auto_pct_doel is not None:      # bij een %-doel rekent de laadstand van de auto zelf
            return 0.0
        if m.auto_geladen_kwh + 0.01 < self._auto_basis:      # nieuwe laadsessie op de paal
            self._auto_basis = 0.0
        return max(m.auto_geladen_kwh - self._auto_basis, 0.0)

    def _zet_autodoel(self, nu: datetime):
        vertrek, kwh, bron = self.autoplan.volgende(nu)
        pct = self.autoplan.doel_pct(nu)
        soc = (self.autoacc.gegevens or {}).get("soc")
        self._auto_pct_doel = pct if (pct is not None and soc is not None) else None
        if pct is not None:
            if soc is not None:
                # laadstand van de auto bekend: precies wat er nog bij moet, plus ~10% laadverlies
                kwh = max(pct - soc, 0.0) / 100 * float(self.cfg["laadpaal"].get("auto_accu_kwh") or 60) * 1.1
            else:
                kwh = float(self.autoplan.d.get("doel_kwh", 15))        # auto (nog) niet bereikbaar: vast aantal kWh
        if vertrek != self._auto_periode:
            # nieuw vertrekmoment: wat er al geladen is, telt voor het vorige
            if self._auto_periode is not None and self.meting:
                self._auto_basis = self.meting.auto_geladen_kwh
            self._auto_periode = vertrek
        self.bed.auto_doel_kwh = round(kwh, 1)
        self.bed.auto_deadline = vertrek.isoformat() if vertrek else None
        self.bed.auto_vertrek = vertrek.astimezone().strftime("%H:%M") if vertrek else ""
        self.bed.auto_zonder = self.autoplan.d.get("zonder_vertrek", "zon")
        self._auto_bron = bron

    def auto_overzicht(self) -> dict:
        """Wat de app over de auto laat zien: wanneer, hoeveel, wat het kost en of het doel gehaald wordt."""
        ks = [k for k in self.plan["kwartieren"] if k.auto_kwh > 0.01]
        nodig = max(self.bed.auto_doel_kwh - self.auto_geladen(), 0.0)
        uit = {"verbonden": bool(self.meting and self.meting.auto_verbonden), "doel_kwh": self.bed.auto_doel_kwh,
               "geladen_kwh": round(self.auto_geladen(), 1), "nog_kwh": round(nodig, 1), "vertrek": self.bed.auto_deadline,
               "bron": getattr(self, "_auto_bron", "schema"), "zonder_vertrek": self.bed.auto_zonder, "laadpaal": self.bed.laadpaal,
               "gepland_kwh": 0.0, "kosten": 0.0, "zon_kwh": 0.0, "van": None, "tot": None, "haalt_doel": True,
               "plan": self.autoplan.dict(), "auto": self.autoacc.status(), "doel_pct": self._auto_pct_doel}
        if ks:
            zon = sum(min(k.auto_kwh, max(k.zon_kwh - k.huis_kwh - k.wp_kwh, 0.0)) for k in ks)
            kosten = sum(min(k.auto_kwh, max(k.zon_kwh - k.huis_kwh - k.wp_kwh, 0.0)) * k.teruglever
                         + max(k.auto_kwh - max(k.zon_kwh - k.huis_kwh - k.wp_kwh, 0.0), 0.0) * k.inkoop for k in ks)
            gepland = sum(k.auto_kwh for k in ks)
            uit.update({"gepland_kwh": round(gepland, 1), "kosten": round(kosten, 2), "zon_kwh": round(zon, 1),
                        "van": ks[0].start.isoformat(), "tot": (ks[-1].start + timedelta(minutes=15)).isoformat(),
                        "haalt_doel": gepland + 0.3 >= nodig or not self.bed.auto_deadline})
        elif self.bed.auto_deadline and nodig > 0.3 and uit["verbonden"]:
            uit["haalt_doel"] = False
        return uit

    def _meld_auto_aangesloten(self):
        """De auto is net aangesloten: op je telefoon kiezen tussen slim laden (goedkoopste uren en zon) en direct laden."""
        a = self.auto_overzicht()
        nodig = a["nog_kwh"] or a["gepland_kwh"]
        k = self.huidig()
        nu_prijs = k.inkoop if k else None
        regels = []
        if a["gepland_kwh"] > 0.3 and a["tot"]:
            klaar = datetime.fromisoformat(a["tot"]).astimezone()
            dag = "vandaag" if klaar.date() == datetime.now().astimezone().date() else "morgen" if klaar.date() == (datetime.now().astimezone() + timedelta(days=1)).date() else ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"][klaar.weekday()]
            gem = a["kosten"] / a["gepland_kwh"]
            regels.append(f"Slim laden: {self._k(a['gepland_kwh'])} kWh, klaar {dag} om {klaar:%H:%M}, ± {self._e(a['kosten'])} (gemiddeld {self._e(gem)} per kWh"
                          + (f", {self._k(a['zon_kwh'])} kWh van de zon" if a["zon_kwh"] > 0.3 else "") + ").")
        if nodig and nu_prijs is not None:
            regels.append(f"Direct laden: ± {self._e(nodig * nu_prijs)} (nu {self._e(nu_prijs)} per kWh).")
        if not regels:
            regels.append("Kies hoe de auto moet laden.")
        stand = {"auto": "slim", "nu": "direct", "zon": "alleen op zon", "stop": "gestopt"}.get(self.bed.laadpaal, self.bed.laadpaal)
        tekst = " ".join(regels) + f" Nu ingesteld: {stand}."
        self.logboek.schrijf("besluit", "Auto aangesloten. " + tekst, "laadpaal")
        self.telefoon.meld("auto-aangesloten", "Energiebrein: auto aangesloten", tekst,
                           acties=[("EB_LAAD_SLIM", "Slim laden"), ("EB_LAAD_NU", "Direct laden"), ("EB_LAAD_ZON", "Alleen zon")])

    async def knop_op_melding(self, actie: str):
        """Antwoord van een knop op een melding."""
        stand = {"EB_LAAD_SLIM": "auto", "EB_LAAD_NU": "nu", "EB_LAAD_ZON": "zon"}.get(actie)
        if not stand:
            return
        self.bed.laadpaal = stand
        self.bewaar_bediening()
        naam = {"auto": "slim laden", "nu": "direct laden", "zon": "alleen op zon"}[stand]
        self.logboek.schrijf("handmatig", f"Laadpaal via de melding op je telefoon op {naam} gezet", "laadpaal")
        self.maak_plan()
        await self.regel()

    def huidig(self) -> Kwartier | None:
        nu = kwartier_start(datetime.now(timezone.utc))
        for k in self.plan["kwartieren"]:
            if k.start == nu:
                return k
        return None

    def herplan(self):
        self._plan_nodig.set()

    # ------------------------------------------------------------ regelen

    async def _eigen(self, m):
        """'Eigen verbruik'. Standaard laat Energiebrein de omvormer dat zelf doen. In P1-regelmodus regelt Energiebrein de accu
        zelf: de P1 moet op ongeveer 0 kW uitkomen, de CT-meter van de omvormer doet er dan niet meer toe."""
        a = self.cfg["accu"]
        if self.cfg["omvormer"].get("regel", "omvormer") != "p1" or not self.huis.p1 or not self.huis.kan_accu_sturen:
            await self.huis.accu("zelfverbruik")
            return
        # Integrerende regeling: de vorige opdracht wordt rustig bijgesteld met wat de P1 nog afwijkt van 0. Zo komt de P1 op 0
        # uit, ook als de omvormer het opgegeven vermogen anders telt dan gedacht (bijvoorbeeld 3x zoveel laadt als gevraagd).
        # Kleine stapjes (0,1 kW) en een lage versterking, anders schiet hij door: van laden uit het net naar terugleveren en terug.
        nu = time.monotonic()
        if getattr(self, "_p1_vorige", None) is None or nu - getattr(self, "_p1_tijd", 0.0) > 90:
            self._p1_vorige = m.accu_kw                         # start vanaf wat de accu nu echt doet
            self._p1_gezet = 0.0
        self._p1_tijd = nu
        gewenst = self._p1_vorige
        snel = abs(m.net_kw) > 1.5                              # groot verschil (waterkoker, wolk weg): meteen bijsturen
        drempel = 0.08 if (m.net_kw > 0 and self._p1_vorige > 0) else 0.2   # laden uit het net: eerder ingrijpen
        if abs(m.net_kw) >= drempel and (snel or nu - self._p1_gezet >= 20):
            gewenst = self._p1_vorige - (0.6 if snel else 0.35) * m.net_kw     # afname: minder laden / meer leveren
        # Laadt de omvormer bij de kleinste opdracht al meer dan er over is, dan haalt hij de rest uit het net. Dat leert de
        # regeling: is het overschot kleiner dan wat de omvormer minimaal laadt, dan rust de accu (een beetje terugleveren is
        # goedkoper dan laden uit het net).
        vorige_kw = getattr(self, "_p1_kw", 0.0)
        if 0 < vorige_kw <= 0.1 and nu - self._p1_gezet >= 20 and m.accu_kw > vorige_kw + 0.15:
            self._p1_min_laden, self._p1_min_tijd = m.accu_kw, nu
        if nu - getattr(self, "_p1_min_tijd", -1e9) > 900:
            self._p1_min_laden = None                           # na een kwartier opnieuw proberen
        nodig = m.accu_kw - m.net_kw                            # zoveel moet de accu doen om het net op 0 te krijgen
        min_laden = getattr(self, "_p1_min_laden", None)
        if gewenst > 0 and min_laden and nodig < min_laden * 0.95:
            gewenst = 0.0
        if m.accu_soc >= float(a.get("max_soc", 100)) and gewenst > 0:
            gewenst = 0.0
        if m.accu_soc <= self.ondergrens() + 0.5 and gewenst < 0:
            gewenst = 0.0
        gewenst = max(-a["max_ontladen_kw"], min(a["max_laden_kw"], gewenst))
        if gewenst != self._p1_vorige:
            self._p1_gezet = nu
        self._p1_vorige = gewenst
        kw = round(gewenst, 1)
        if abs(kw) < 0.1:
            kw = 0.0
        self._p1_kw = kw
        log.debug("P1-regeling: net %.2f kW, accu %.2f kW, opdracht %.1f kW", m.net_kw, m.accu_kw, kw)
        self._p1_tekst = (f"P1-regeling: laadt {kw:.1f} kW".replace(".", ",") if kw > 0.01 else
                          f"P1-regeling: levert {-kw:.1f} kW".replace(".", ",") if kw < -0.01 else "P1-regeling: accu rust")
        if kw > 0.01:
            await self.huis.accu("laden", kw, a.get("max_soc", 100))
        elif kw < -0.01:
            await self.huis.accu("ontladen", -kw)
        else:
            await self.huis.accu("vasthouden", 0, m.accu_soc)

    async def regel(self):
        m = await self.huis.lees()
        vorige_verbonden = self.meting.auto_verbonden if self.meting else False
        self.meting = m
        if m.auto_verbonden != vorige_verbonden:
            if m.auto_verbonden and not vorige_verbonden and getattr(self, "_gestart", False):
                self._auto_net_aan = True             # na het nieuwe plan een melding met de keuze slim of direct laden
            self.herplan()
        k = self.huidig()
        nu = datetime.now(timezone.utc)
        beurs = self.prijzen.beurs.get(kwartier_start(nu), 0.1)
        inkoop, terug = self.prijzen.inkoop(beurs, kwartier_start(nu)), self.prijzen.teruglever(beurs, kwartier_start(nu))
        dt = (nu - self._laatst_gemeten).total_seconds() if self._laatst_gemeten else 0.0
        self._laatst_gemeten = nu
        if 0 < dt < 120:
            self.gesch.meet(m, inkoop, terug, dt, beurs)
        self._vergelijk_meters(m, nu)

        b = {}
        self._p1_tekst = None
        a = self.cfg["accu"]
        # Uitleg onder het besluit. Standaard die van het plan; wijkt het besluit van dit moment af van het plan,
        # dan een eigen uitleg (anders staat er bijvoorbeeld 'houdt lading vast' met daaronder 'accu voedt het huis').
        reden = k.reden if k else ""
        grens = self.ondergrens()
        leeg = m.accu_soc <= grens + 0.5
        leeg_tekst = f"Accu is leeg (op de ondergrens van {grens:.0f}%)"
        # --- accu ---
        if self.klemtest.bezig:
            # Proef met de CT-klemmen: de proef bepaalt even wat de accu doet.
            b["accu"] = await self.klemtest.ronde(m)
            reden = "Na de proef gaat alles weer vanzelf verder zoals gepland."
        elif self.bed.accu != "auto":
            actie = self.bed.accu
            if actie == "zelfverbruik":
                await self._eigen(m)
            else:
                kw = a["max_laden_kw"] if actie == "laden" else a["max_ontladen_kw"]
                await self.huis.accu(actie, kw, a["max_soc"] if actie == "laden" else m.accu_soc)
            b["accu"] = "Handmatig: " + {"laden": "laden", "ontladen": "leveren", "vasthouden": "vasthouden",
                                         "zelfverbruik": "eigen verbruik"}.get(actie, actie)
            reden = "Je hebt dit zelf gekozen bij Bediening. Kies daar Slim om het brein weer te laten beslissen."
        elif k and m.auto_kw > 1.0 and k.accu_actie != "laden" and m.zon_kw - m.huis_kw - m.wp_kw < m.auto_kw * 0.5 \
                and not (k.accu_actie == "ontladen" and k.net_kwh < -0.02):
            # De auto laadt uit het net. De accu zou hem anders vanzelf meevoeden (de omvormer ziet alleen 'huis vraagt stroom').
            # Dat is zonde: wat in de accu zit is later meer waard dan de stroom waarmee de auto nu goedkoop laadt.
            await self.huis.accu("vasthouden", 0, m.accu_soc)
            b["accu"] = ("Houdt lading vast: de auto laadt nu uit het net, niet uit de accu" if self.huis.kan_vasthouden
                         else "Voedt het huis (vasthouden kan bij dit merk nog niet, dus de accu helpt de auto mee)")
            reden = "Wat in de accu zit, is straks meer waard dan de stroom waarmee de auto nu laadt."
        elif m.accu_soc >= float(a.get("max_soc", 100)) and self.huis.kan_vasthouden \
                and m.zon_kw - (m.huis_kw + m.wp_kw + m.auto_kw) > (0.0 if getattr(self, "_vol_vast", False) else 0.1) \
                and not (k and k.accu_actie == "ontladen"):
            # De omvormer laadt in eigen verbruik door tot zijn eigen maximum (vaak 100%). Is de ingestelde bovengrens
            # bereikt en heeft de zon overschot, dan houden we de lading vast: de zon voedt het huis, de rest gaat naar het net.
            self._vol_vast = True
            await self.huis.accu("vasthouden", 0, m.accu_soc)
            b["accu"] = f"Accu is vol tot je grens van {a['max_soc']:.0f}%: lading vastgehouden"
            reden = "De zon voedt het huis; wat over is, gaat naar het net."
        elif k:
            self._vol_vast = False
            vraag = k.huis_kwh + k.wp_kwh + k.auto_kwh - k.zon_kwh
            # Wat het plan voor dit kwartier wil laden, en wat de zon NU echt over heeft. Het plan rekent met de
            # zonvoorspelling; is het bewolkter dan verwacht, dan moet de rest uit het net komen, anders haalt de accu
            # zijn doel niet en mis je straks de dure uren.
            plan_kw = max(k.accu_kwh, 0.0) / 0.25 / math.sqrt(float(a.get("rendement", 0.9)))
            overschot_nu = m.zon_kw - (m.huis_kw + m.wp_kw + m.auto_kw)
            # Zonne-overschot gaat altijd eerst de accu in zolang die ruimte heeft (instelling, standaard aan). Ook als het plan
            # iets anders verwachtte: de zonvoorspelling zit er vaak naast, en wat de zon NU over heeft, telt.
            zon_eerst = (zon_in_accu(a) and overschot_nu > 0.15 and m.accu_soc < float(a.get("max_soc", 100)) - 0.5
                         and k.accu_actie != "laden" and not (k.accu_actie == "ontladen" and k.net_kwh < -0.02)
                         )
            if zon_eerst:
                await self._eigen(m)
                b["accu"] = "Laadt met zonne-overschot"
                reden = self._zon_reden(m) or "Wat de zon over heeft, gaat eerst de accu in (zo ingesteld bij Koppelen)."
            elif k.accu_actie == "laden" and vraag < -0.02 and overschot_nu >= 0.8 * plan_kw:
                await self._eigen(m)
                b["accu"] = "Laadt met zonne-overschot"
                reden = self._zon_reden(m) or reden
            elif k.accu_actie == "laden" and vraag < -0.02 and self.prijzen.bron == "model":
                await self._eigen(m)
                b["accu"] = "Laadt met wat de zon over heeft (minder zon dan verwacht; geen echte prijzen om uit het net bij te laden)"
                reden = ""
            elif k.accu_actie == "laden" and self.prijzen.bron == "model":
                # Zonder echte stroomprijzen nooit uit het net laden: het plan rekent dan met een geschat patroon.
                await self._eigen(m)
                b["accu"] = "Laadt niet uit het net: er zijn nu geen echte stroomprijzen"
                reden = ""
            elif k.accu_actie == "laden":
                kw = min(plan_kw, a["max_laden_kw"])
                await self.huis.accu("laden", kw, k.soc_na)
                if vraag < -0.02:
                    b["accu"] = (f"Laadt {self._k(kw)} kW tot {k.soc_na:.0f}%: minder zon dan verwacht "
                                 f"({self._k(max(m.zon_kw, 0))} kW), de rest komt goedkoop uit het net")
                else:
                    b["accu"] = f"Laadt uit het net ({self._k(kw)} kW) tot {k.soc_na:.0f}%"
            elif k.accu_actie == "ontladen" and leeg:
                await self._eigen(m)
                b["accu"] = leeg_tekst
                reden = "Het huis haalt de stroom nu van het net."
            elif k.accu_actie == "ontladen" and k.net_kwh < -0.02:
                # Eerst het huis, dan pas terugleveren: wat het huis NU vraagt kost inkoopprijs, terugleveren levert
                # alleen het (lagere) terugleverbedrag op. Het plan rekende met een verwacht verbruik; het echte
                # verbruik van dit moment gaat voor.
                plan_terug = max(-k.net_kwh / 0.25, 0.0)
                vraag_nu = max(m.huis_kw + m.wp_kw + m.auto_kw - m.zon_kw, 0.0)
                # Veiligheidsregel: alleen terugverkopen als dat meer oplevert dan later goedkoper weer inkopen, na laadverlies,
                # slijtage en de minimale winst. Of als de accu de komende 24 uur toch vol raakt (dan is de lading anders niets waard).
                komend = [x for x in self.plan["kwartieren"] if x.start + timedelta(minutes=15) > nu and x.start < nu + timedelta(hours=24)]
                goedkoopst = min((x.inkoop for x in komend), default=inkoop)
                raakt_vol = any(x.soc_na >= a["max_soc"] - 2 for x in komend)
                rendement = float(a.get("rendement", 0.9))
                drempel = float(a.get("slijtage_per_kwh", 0.02)) + max(float(a.get("min_winst_per_kwh", 0.0) or 0.0), 0.0)
                verkopen = mag_verkopen(a)
                if not verkopen or (terug * rendement - drempel < goedkoopst and not raakt_vol):
                    plan_terug = 0.0
                kw = min(a["max_ontladen_kw"], vraag_nu + plan_terug)
                kw = max(round(kw / 0.4) * 0.4, 0.4)            # in stapjes van 0,4 kW, zodat de omvormer niet elke 10 s een nieuwe opdracht krijgt
                if plan_terug == 0.0:
                    await self._eigen(m)
                    b["accu"] = "Voedt het huis"
                    reden = ("Terugverkopen aan het net staat uit; de accu voedt alleen je eigen huis." if not verkopen else
                             f"Terugverkopen voor {self._e(terug)} levert na laadverlies en slijtage te weinig op tegenover later "
                             f"weer inkopen (vanaf {self._e(goedkoopst)}).")
                elif kw <= vraag_nu + 0.05:
                    await self._eigen(m)
                    b["accu"] = f"Voedt het huis ({self._k(vraag_nu)} kW)"
                    reden = "Het huis gaat voor terugleveren: wat het huis nu vraagt, kost de inkoopprijs."
                else:
                    await self.huis.accu("ontladen", kw)
                    if m.accu_kw < -0.3:
                        # wat er echt gebeurt, niet wat er gevraagd is: de accu kan minder leveren dan het vermogen van de omvormer
                        b["accu"] = f"Verkoopt {self._k(max(-m.net_kw, 0.0))} kW aan het net en voedt het huis"
                        grens_kw = self._accu_grens_kw()
                        if -m.accu_kw < 0.85 * kw and grens_kw and -m.accu_kw >= 0.9 * grens_kw:
                            pct = int(grens_kw / float(self.cfg["omvormer"].get("vermogen_kw") or kw) * 100 // 5 * 5)
                            reden = (reden + " " if reden else "") + (
                                f"De accu levert {self._k(-m.accu_kw)} kW: meer laat de accu zelf nu niet toe. "
                                f"Zet bij Koppelen het ontlaadvermogen op {pct}%, dan rekent het plan daarmee.")
                    else:
                        b["accu"] = f"Gaat {self._k(kw)} kW leveren: aan het huis en {self._k(kw - vraag_nu)} kW aan het net"
            elif k.accu_actie == "ontladen":
                await self._eigen(m)
                b["accu"] = "Voedt het huis"
            elif self._voeden(m, k, inkoop):
                # Het huis vraagt nu stroom en die is duurder dan wat een opgeslagen kWh later oplevert: voeden.
                await self._eigen(m)
                b["accu"] = "Voedt het huis"
                reden = f"Stroom kost nu {self._e(inkoop)}; een kWh in de accu is later {self._e(k.waarde_kwh)} waard."
            elif vraag > 0 or m.huis_kw + m.wp_kw + m.auto_kw > m.zon_kw:
                await self.huis.accu("vasthouden", 0, m.accu_soc)
                if leeg:
                    b["accu"] = leeg_tekst
                    reden = reden if "leeg" in reden else "Het huis haalt de stroom nu van het net."
                elif self.huis.kan_vasthouden:
                    b["accu"] = "Houdt lading vast voor een duurder moment"
                    straks = next((x for x in self.plan["kwartieren"] if x.start > k.start and x.accu_actie == "ontladen"), None)
                    eta = math.sqrt(float(a.get("rendement", 0.9)))
                    if straks and straks.inkoop > inkoop + 0.005:
                        reden = (f"Om {straks.start.astimezone().strftime('%H:%M')} kost stroom {self._e(straks.inkoop)}; "
                                 f"dan levert de lading meer op dan nu ({self._e(inkoop)}).")
                    else:
                        reden = (f"Een kWh uit de accu bespaart nu {self._e(inkoop * eta)} (na {round((1 - eta) * 100)}% verlies bij het "
                                 f"ontladen); bewaard is hij straks {self._e(k.waarde_kwh)} waard.")
                else:
                    b["accu"] = "Voedt het huis (lading vasthouden kan bij dit merk nog niet veilig)"
                    reden = ""
            else:
                await self._eigen(m)
                if overschot_nu > 0.1 and m.accu_soc < float(a.get("max_soc", 100)) - 0.5:
                    b["accu"] = "Laadt met zonne-overschot"
                    reden = self._zon_reden(m) or "De zon levert meer dan het huis gebruikt; dat gaat de accu in in plaats van het net op."
                else:
                    b["accu"] = "Eigen verbruik"
        if self._p1_tekst and "accu" in b:
            b["accu"] += f" · {self._p1_tekst}"
        if "accu" in b and not self.huis.kan_accu_sturen:
            b["accu"] = "Advies: " + b["accu"][0].lower() + b["accu"][1:] + " (dit merk wordt nog alleen uitgelezen)"
        # --- zonnepanelen: aftoppen als terugleveren geld kost ---
        aftoppen = self.bed.zon == "auto" and terug < 0 and self.cfg["omvormer"].get("aftoppen_bij_negatieve_prijs", True)
        await self.huis.exportlimiet(0 if aftoppen else None)
        b["zon"] = "Afgetopt: terugleveren kost nu geld" if aftoppen else "Normaal"
        # --- laadpaal ---
        b["laadpaal"] = await self._laadpaal(m, k)
        # --- warmtepomp ---
        if self.cfg["warmtepomp"].get("type", "uit") != "uit":
            stand = self.bed.warmtepomp if self.bed.warmtepomp != "auto" else (k.wp_stand if k else "normaal")
            await self.huis.warmtepomp(stand)
            b["warmtepomp"] = {"boost": "Stookt extra op goedkope stroom", "wacht": "Wacht even, stroom is duur",
                               "normaal": "Normaal"}[stand]
        if self.huis.vergrendeld:
            for sleutel in ("accu", "zon", "laadpaal", "warmtepomp"):
                if sleutel in b and not b[sleutel].startswith("Advies") and b[sleutel] not in ("Normaal", "Niet aangesloten", "Geen auto aangesloten"):
                    b[sleutel] = "Advies: " + b[sleutel][0].lower() + b[sleutel][1:]
        namen = {"accu": "Thuisaccu", "laadpaal": "Laadpaal", "warmtepomp": "Warmtepomp", "zon": "Zonnepanelen"}
        for sleutel, naam in namen.items():
            if sleutel in b and b[sleutel] and b[sleutel] not in ("Niet aangesloten",):
                self.logboek.als_anders("besluit_" + sleutel, "besluit", f"{naam}: {b[sleutel]}", sleutel)
        self._volg_storingen()
        b["reden"] = reden
        b["inkoop"], b["teruglever"], b["beurs"] = inkoop, terug, beurs
        self.besluit = b

    def _accu_grens_kw(self) -> float | None:
        """Hoeveel de accu zelf (de BMS) nu maximaal mag leveren: toegestane stroom × accuspanning, als de omvormer dat meldt."""
        bms = getattr(self.huis.omvormer, "bms", None) or {}
        try:
            a, v = float(bms.get("ontladen_a") or 0), float(bms.get("spanning") or 0)
        except (TypeError, ValueError):
            return None
        return a * v / 1000 if a > 0 and v > 20 else None

    def _vergelijk_meters(self, m: Meting, nu: datetime) -> None:
        """Bijhouden of de CT-meter van de omvormer en de slimme meter hetzelfde zien. Eén meting zegt niets: de slimme meter
        meet elke seconde, de omvormer loopt een paar seconden achter. Een waterkoker of warmtepomp die aan- of uitgaat,
        geeft dan even een groot verschil zonder dat er iets mis is."""
        reeks = self._meter_reeks
        while reeks and (nu - reeks[0][0]).total_seconds() > 600:
            reeks.popleft()
        mk = getattr(self.huis.omvormer, "meter_kw", None)
        if mk is None or not self.huis.p1 or self.cfg["omvormer"].get("meter_meet", "net") == "huis":
            reeks.clear()
            return
        if m.net_kw > 0.8 or mk > 0.8:           # alleen als er echt iets van het net komt; bij weinig verbruik zegt het verschil weinig
            reeks.append((nu, m.net_kw, mk, getattr(self.huis.p1, "fasen", None), getattr(self.huis.omvormer, "meter_fasen", None)))

    def _meters_wijken_af(self) -> tuple[float, float] | None:
        """Wijken de meters al minstens twee minuten (aan metingen) meestal meer dan 1 kW af? Dan de gemiddelden, anders None."""
        reeks = list(self._meter_reeks)
        if len(reeks) < 12:
            return None
        af = [r for r in reeks if abs(r[1] - r[2]) > 1.0]
        if len(af) < 0.6 * len(reeks):
            return None
        return sum(r[1] for r in af) / len(af), sum(r[2] for r in af) / len(af)

    def _fase_tekst(self) -> str:
        """Per fase vergelijken: zo zie je welke CT-klem niet klopt. Een klem die andersom zit, meet hetzelfde maar met het
        omgekeerde teken; een klem die niet om de kabel zit, meet niets."""
        rijen = [r for r in self._meter_reeks if len(r) > 4 and r[3] and r[4]]
        if len(rijen) < 6:
            return ""
        p1 = [sum(r[3][i] for r in rijen) / len(rijen) for i in range(3)]
        ct = [sum(r[4][i] for r in rijen) / len(rijen) for i in range(3)]
        delen, tips = [], []
        d = [ct[i] - p1[i] for i in range(3)]
        gelijk = all(x > 0.2 for x in d) or all(x < -0.2 for x in d)
        if gelijk and max(abs(x) for x in d) < 2.5 * min(abs(x) for x in d):
            # Alle drie de fasen even ver ernaast: dan ligt het niet aan één klem, maar aan wat de klemmen samen zien.
            tips.append(f"op alle drie de fasen ziet de GoodWe ongeveer evenveel {'meer afname' if d[0] > 0 else 'minder afname'} "
                        f"(samen {self._k(abs(sum(d)))} kW). Dan zien de klemmen waarschijnlijk de stroom van de omvormer zelf niet: "
                        "is de omvormer aangesloten tussen de meter en de klemmen, of zitten de klemmen om een andere kabel dan de "
                        "hoofdkabel?")
        for i in range(3):
            delen.append(f"L{i + 1}: slimme meter {self._k(p1[i])}, GoodWe {self._k(ct[i])}")
            if gelijk:
                continue
            if abs(p1[i]) > 0.2 and abs(ct[i] + p1[i]) < 0.35 * abs(p1[i]) and abs(ct[i] - p1[i]) > 0.4:
                tips.append(f"de klem op L{i + 1} zit waarschijnlijk andersom (zelfde waarde, omgekeerd teken)")
            elif abs(p1[i]) > 0.3 and abs(ct[i]) < 0.05:
                tips.append(f"de klem op L{i + 1} meet niets: zit hij om de kabel?")
            elif abs(ct[i] - p1[i]) > 0.4:
                tips.append(f"L{i + 1} wijkt af: zit die klem op de goede fase?")
        tekst = " Per fase (kW, + = afname): " + "; ".join(delen) + "."
        if tips:
            tips_tekst = "; ".join(tips)
            tekst += " Mogelijke oorzaak: " + tips_tekst + ("" if tips_tekst.endswith("?") else ".") + " (De fasenummers van de omvormer kunnen anders zijn dan die van de meter.)"
        return tekst

    @staticmethod
    def _k(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    @staticmethod
    def _e(x: float) -> str:
        return f"€ {x:.2f}".replace(".", ",")

    def _zon_reden(self, m: Meting) -> str | None:
        """Bij laden met zonne-overschot: zeg wat er echt gebeurt als dat afwijkt van de bedoeling."""
        min_laden = getattr(self, "_p1_min_laden", None)
        if m.accu_kw > 0.1 and m.net_kw > 0.15:
            return (f"De accu laadt {self._kw(m.accu_kw)}, waarvan {self._kw(m.net_kw)} nu uit het net komt. "
                    "De regeling stuurt bij tot alleen de zon de accu laadt.")
        if m.accu_kw < 0.1 and m.net_kw < -0.1 and min_laden:
            return (f"De omvormer laadt minimaal {self._kw(min_laden)}; de zon heeft maar {self._kw(-m.net_kw)} over. "
                    "Dan zou de rest uit het net komen, dus de accu wacht tot er meer zon is.")
        if m.accu_kw < 0.1 and m.net_kw < -0.1:
            return (f"Er gaat {self._kw(-m.net_kw)} naar het net; de accu laadt nog niet. "
                    + ("De regeling stuurt bij." if self._p1_tekst else
                       "Ziet de CT-meter van de omvormer iets anders dan je slimme meter? Zet dan de P1-regeling aan."))
        return None

    @staticmethod
    def _kw(x: float) -> str:
        return f"{x:.1f} kW".replace(".", ",")

    def _voeden(self, m: Meting, k: Kwartier, inkoop: float) -> bool:
        """Moet de accu de vraag van dit moment dekken? Ja als een kWh uit de accu nu meer bespaart
        dan hij later in het plan oplevert, en er boven het minimum genoeg in zit."""
        vraag_nu = m.huis_kw + m.wp_kw + m.auto_kw - m.zon_kw
        if vraag_nu <= 0.05 or m.accu_soc <= self.ondergrens() + 1:
            return False
        eta = math.sqrt(self.cfg["accu"]["rendement"])
        return inkoop * eta > k.waarde_kwh + 0.005

    async def _laadpaal(self, m: Meting, k: Kwartier | None) -> str:
        c = self.cfg["laadpaal"]
        if c.get("type", "uit") == "uit":
            return "Niet aangesloten"
        fasen, amin, amax = c["fasen"], c["min_stroom_a"], c["max_stroom_a"]
        naar_a = lambda kw: kw * 1000 / (230 * fasen)
        if not m.auto_verbonden:
            await self.huis.laadstroom(0)
            return "Geen auto aangesloten"
        soc = (self.autoacc.gegevens or {}).get("soc")
        klaar = (soc >= self._auto_pct_doel) if (self._auto_pct_doel is not None and soc is not None) \
            else self.auto_geladen(m) >= self.bed.auto_doel_kwh
        modus = self.bed.laadpaal
        if modus == "stop":
            a, txt = 0, "Gestopt"
        elif modus == "nu":
            a, txt = amax, "Laadt op vol vermogen"
        else:
            overschot = m.auto_kw - m.net_kw   # wat er naar de auto kan zonder af te nemen
            if modus == "zon":
                a = math.floor(naar_a(overschot))
                a, txt = (a, "Laadt op zonne-overschot") if a >= amin else (0, "Wacht op zonne-overschot")
            elif k and k.auto_kwh > 0 and not klaar:
                if k.zon_kwh - k.huis_kwh - k.wp_kwh > 0.05:
                    a = max(amin, math.floor(naar_a(overschot)))
                    txt = "Laadt op zon"
                else:
                    a = round(naar_a(k.auto_kwh / 0.25))
                    txt = f"Laadt op goedkoop uur ({self._e(self.besluit.get('inkoop', 0))} per kWh)"
            else:
                a, txt = 0, "Klaar" if klaar else "Wacht op goedkoper moment"
        a = min(max(a, 0), amax)
        # piekbewaking: nooit boven je aansluiting
        ruimte = self.cfg["net"]["max_afname_kw"] - (m.net_kw - m.auto_kw)
        if a and naar_a(ruimte) < a:
            a = max(math.floor(naar_a(ruimte)), 0)
            a = a if a >= amin else 0
            txt += " (afgeknepen: piekbewaking)"
        await self.huis.laadstroom(a)
        return txt

    # ------------------------------------------------------------ lussen
    async def _auto_lus(self):
        while True:
            try:
                oud = (self.autoacc.gegevens or {}).get("soc")
                await self.autoacc.ververs()
                nieuw = (self.autoacc.gegevens or {}).get("soc")
                if nieuw is not None and (oud is None or abs(nieuw - oud) >= 3):
                    self.herplan()
            except Exception:
                log.exception("Auto uitlezen mislukt")
            await asyncio.sleep(60)

    async def _regel_lus(self):
        while True:
            try:
                await self.regel()
            except Exception:
                log.exception("Regelen mislukt")
            # tijdens de klemmenproef vaker meten
            await asyncio.sleep(3 if self.klemtest.bezig else self.cfg["regelaar"]["interval_s"])

    async def _plan_lus(self):
        while True:
            try:
                await asyncio.wait_for(self._plan_nodig.wait(), timeout=self.cfg["regelaar"]["plan_interval_min"] * 60)
            except asyncio.TimeoutError:
                pass
            self._plan_nodig.clear()
            try:
                self.maak_plan()
            except Exception:
                log.exception("Plannen mislukt")

    async def _data_lus(self):
        while True:
            await asyncio.sleep(3600)
            try:
                await self.prijzen.ververs()       # na 13:00 komen de prijzen van morgen erbij
                await self.voorsp.ververs_zon()
                await self.voorsp.weer.ververs()
                self.voorsp.leer()
                self._bereken_conditie()
                self.herplan()
            except Exception:
                log.exception("Data verversen mislukt")

    def _volg_storingen(self):
        """Nieuwe storingen en opgeloste storingen in het logboek zetten."""
        try:
            nu = {x["titel"]: x for x in self.storingen() if x["ernst"] != "info"}
        except Exception:
            return
        vorig = self._storingen_vorig or {}
        for titel, x in nu.items():
            if titel not in vorig:
                self.logboek.schrijf("storing", f"{titel}. {x['uitleg']}", x.get("apparaat"))
                if self._storingen_vorig is not None:  # niet bij het opstarten alles opnieuw melden
                    self.telefoon.meld(titel, f"Energiebrein: {titel}", x["uitleg"], belangrijk=x["ernst"] == "fout")
        for titel, x in vorig.items():
            if titel not in nu:
                self.logboek.schrijf("opgelost", f"Opgelost: {titel}", x.get("apparaat"))
                if self.telefoon.bij_opgelost:
                    self.telefoon.meld(titel, "Energiebrein: opgelost", f"Geldt niet meer: {titel}.")
        self._storingen_vorig = nu

    # ------------------------------------------------------------ voor de app
    def storingen(self) -> list[dict]:
        """Alles wat niet goed gaat, in gewone taal, met wat je eraan kunt doen.
        ernst: 'fout' = werkt niet, 'let op' = werkt beperkt, 'info' = goed om te weten."""
        namen = {"omvormer": "Omvormer en accu", "p1": "Slimme meter", "laadpaal": "Laadpaal", "warmtepomp": "Warmtepomp"}
        uit = []
        for sleutel, tekst in self.huis.fouten.items():
            basis = sleutel.replace("_opdracht", "")
            uit.append({"ernst": "fout", "apparaat": basis, "titel": f"{namen.get(basis, basis)}: " + ("opdracht lukt niet" if sleutel.endswith("_opdracht") else "geen verbinding"),
                        "uitleg": tekst, "actie": "Controleer het IP-adres en test de verbinding bij Koppelen."})
        for sleutel, tekst in self.huis.meldingen.items():
            if sleutel not in self.huis.fouten:
                uit.append({"ernst": "let op", "apparaat": sleutel, "titel": f"{namen.get(sleutel, sleutel)}: alleen uitlezen", "uitleg": tekst,
                            "actie": "Zet sturen aan bij Koppelen zodra het uitlezen klopt."})
        omv, m = self.huis.omvormer, self.meting
        bms = getattr(omv, "bms", None) or {}
        if bms.get("ontladen_a") == 0 and m and m.accu_soc > self.cfg["accu"]["min_soc"] + 5:
            uit.append({"ernst": "fout", "apparaat": "omvormer", "titel": "De accu zelf staat ontladen niet toe",
                        "uitleg": f"De accu (BMS) meldt dat hij 0 A mag ontladen, terwijl hij {m.accu_soc:.0f}% vol is. "
                                  "Dan levert hij niets, wat de omvormer of Energiebrein ook vraagt"
                                  + (f". Accutemperatuur {bms['temperatuur']} °C" if bms.get("temperatuur") is not None else "")
                                  + (f". Melding van de accu: {bms['fout']}" if bms.get("fout") else "") + ".",
                        "actie": "Kijk in de GoodWe-app bij de batterij naar meldingen. Blijft het zo, neem dan contact op met je installateur of de leverancier van de accu."})
        st = getattr(omv, "stand", {}) or {}
        grens = st.get("ondergrens_pct")
        if grens is not None and grens >= self.cfg["accu"]["min_soc"] + 2:
            uit.append({"ernst": "let op" if not (m and m.accu_soc <= grens + 1) else "fout", "apparaat": "omvormer",
                        "titel": f"De omvormer ontlaadt de accu niet verder dan {grens}%",
                        "uitleg": f"In de GoodWe staat de ontlaaddiepte (DoD) op {100 - grens}%, dus de accu stopt bij {grens}%. "
                                  f"Energiebrein rekent met {self.cfg['accu']['min_soc']:.0f}%. Daardoor blijft er {grens - self.cfg['accu']['min_soc']:.0f}% "
                                  "van je accu ongebruikt en haal je stroom van het net terwijl de accu nog lading heeft.",
                        "actie": "Druk op de knop om de ondergrens in de omvormer gelijk te zetten aan Energiebrein, of pas de ontlaaddiepte aan in de SolarGo-app.",
                        "knop": {"tekst": f"Ondergrens op {max(10, int(self.cfg['accu']['min_soc']))}% zetten", "actie": "ondergrens",
                                 "waarde": max(10, int(self.cfg["accu"]["min_soc"]))}})
        afwijking = self._meters_wijken_af()
        if afwijking and self.cfg["omvormer"].get("meter_meet", "net") != "huis":
            p1, ct = afwijking
            uit.append({"ernst": "let op", "apparaat": "omvormer", "titel": "De meter van de GoodWe ziet iets anders dan je slimme meter",
                        "uitleg": f"De afgelopen minuten mat je slimme meter gemiddeld {self._k(p1)} kW van het net, de CT-meter van de GoodWe "
                                  f"{self._k(ct)} kW. De accu voedt alleen wat zijn eigen meter ziet. Zit die meter niet op de hoofdaansluiting "
                                  "(alle fasen), dan blijft de accu stil terwijl je huis van het net haalt." + self._fase_tekst(),
                        "actie": "Laat controleren of de CT-klemmen van de GoodWe om de hoofdkabels in de meterkast zitten, op alle drie de fasen en in de goede richting."})
        afw = getattr(self.huis.omvormer, "afwijking", None)
        if afw:
            st = getattr(self.huis.omvormer, "stand", {})
            uit.append({"ernst": "fout", "apparaat": "omvormer", "titel": "Accu doet iets anders dan Energiebrein vraagt",
                        "uitleg": afw + " Stand nu: " + ", ".join(f"{k} {v}" for k, v in st.items()) + ".",
                        "actie": "Zet in de SolarGo-app de werkmodus op Algemeen en controleer of je energieleverancier je accu op afstand bestuurt; zo ja, zet dat daar uit."})
        if self.prijzen.tibber_fout:
            uit.append({"ernst": "fout", "apparaat": "contract", "titel": "Tibber: je prijzen komen niet binnen", "uitleg": self.prijzen.tibber_fout,
                        "actie": "Controleer je Tibber-code bij Koppelen → Contract en druk op Test."})
        if self.prijzen.bron == "model":
            uit.append({"ernst": "let op", "apparaat": "contract", "titel": "Geen actuele stroomprijzen",
                        "uitleg": "De beursprijzen konden niet worden opgehaald. Het brein rekent nu met een standaard dagpatroon.",
                        "actie": "Controleer of dit apparaat internet heeft. Het brein probeert het elk uur opnieuw."})
        if self.autoacc.aan and self.autoacc.fout:
            uit.append({"ernst": "let op", "apparaat": "auto", "titel": "Je auto is niet uit te lezen", "uitleg": self.autoacc.fout,
                        "actie": "Controleer bij Koppelen → Auto je inlog. Tot het werkt rekent het brein met een vast aantal kWh."})
        lic = self.licentie.status()
        if self.huis.vergrendeld:
            uit.append({"ernst": "let op", "apparaat": "abonnement", "titel": "Gratis versie: het brein geeft advies, maar stuurt niets aan",
                        "uitleg": lic["tekst"], "actie": "Vul je licentiecode in bij Koppelen → Abonnement om automatisch te laten sturen."})
        elif lic.get("fout") and not lic.get("eigen_gebruik"):
            uit.append({"ernst": "info", "apparaat": "abonnement", "titel": "Licentie kan nu niet worden gecontroleerd", "uitleg": lic["tekst"], "actie": ""})
        kwp = sum(float(v.get("kwp") or 0) for v in self.cfg.get("zonnepanelen", []))
        if kwp > 60:
            uit.append({"ernst": "fout", "apparaat": "zon", "titel": f"Zonnepanelen: {kwp:,.1f} kWp is veel te veel".replace(",", "X").replace(".", ",").replace("X", "."),
                        "uitleg": "Daardoor kloppen de zonvoorspelling en het plan niet. Een woning heeft meestal 3 tot 15 kWp.",
                        "actie": "Controleer bij Koppelen → Zonnepanelen het vermogen per dakvlak (aantal panelen × wattpiek ÷ 1000)."})
        if self.voorsp.zon_bron == "model" and not self.huis.sim:
            uit.append({"ernst": "info", "apparaat": "zon", "titel": "Geen zonvoorspelling",
                        "uitleg": "De weersvoorspelling voor je panelen is niet bereikbaar; het brein gebruikt het maandgemiddelde.", "actie": ""})
        uit.extend(self.conditie.storingen())
        return uit

    def _bereken_conditie(self):
        try:
            self.conditie.bereken(getattr(self.huis.omvormer, "bms", None))
        except Exception:
            log.exception("Conditie van de accu uitrekenen mislukt")
        try:
            self.terugblik.doe(self)          # gisteren achteraf, als dat nog niet is uitgerekend
        except Exception:
            log.exception("Terugblik mislukt")

    def waarde_lading(self) -> float:
        """Wat een kWh die nu in de accu zit straks ongeveer waard is: de gemiddelde inkoopprijs van het komende etmaal,
        min het verlies bij het ontladen. Hiermee telt lading die vandaag is ingekocht maar nog niet gebruikt eerlijk mee."""
        prijzen = sorted(k.inkoop for k in self.plan["kwartieren"][:96])
        if not prijzen:
            return 0.0
        return prijzen[len(prijzen) // 2] * math.sqrt(float(self.cfg["accu"].get("rendement", 0.9)))

    def vandaag(self) -> dict:
        v = self.gesch.vandaag()
        m, cap = self.meting, float(self.cfg["accu"]["capaciteit_kwh"])
        verschil = (m.accu_soc - v["soc_begin"]) / 100 * cap if (m and v.get("soc_begin") is not None) else 0.0
        v["lading_kwh"] = round(verschil, 1)                       # zoveel meer (of minder) zit er nu in de accu dan vannacht
        v["lading_eur"] = round(verschil * self.waarde_lading(), 2)
        v["bespaard_accu"] = round(v["kosten_zonder_accu"] - v["kosten"] + v["lading_eur"], 2)
        return v

    def status(self) -> dict:
        m = self.meting
        return {"meting": m.dict() if m else None, "besluit": self.besluit, "bediening": self.bed.dict(),
                "vandaag": self.vandaag(), "voorspelling": self.voorsp.status(),
                "accu_instelling": {"min": self.ondergrens(), "max": float(self.cfg["accu"].get("max_soc", 100)),
                                    "min_ingesteld": float(self.cfg["accu"]["min_soc"]), "min_omvormer": self._omvormer_grens(),
                                    "terugverkopen": mag_verkopen(self.cfg["accu"]),
                                    "min_winst": float(self.cfg["accu"].get("min_winst_per_kwh", 0.0) or 0.0)},
                "klemtest": self.klemtest.dict(), "conditie": self.conditie.laatst, "terugblik": self.terugblik.status(), "storm": getattr(self, "storm_info", None),
                "fasen": {"p1": getattr(self.huis.p1, "fasen", None), "omvormer": getattr(self.huis.omvormer, "meter_fasen", None)},
                "prijsbron": self.prijzen.bron, "simulatie": bool(self.huis.sim),
                "apparaten": self.huis.onderdelen(), "fouten": self.huis.fouten, "meldingen": self.huis.meldingen,
                "storingen": self.storingen(), "tibber": {"actief": bool(self.prijzen.tibber), "fout": self.prijzen.tibber_fout},
                "accu_kwh": self.cfg["accu"]["capaciteit_kwh"], "max_afname_kw": self.cfg["net"]["max_afname_kw"],
                "plan_samenvatting": self.plan["samenvatting"], "licentie": self.licentie.status(),
                "auto": self.auto_overzicht(),
                "accu_temp": (getattr(self.huis.omvormer, "bms", None) or {}).get("temperatuur")}

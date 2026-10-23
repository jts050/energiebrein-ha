"""Het brein: haalt prijzen en voorspellingen op, maakt elk kwartier een plan en stuurt elke paar seconden bij."""
from __future__ import annotations
import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .apparaten import Huis
from .config import data_map
from .geschiedenis import Geschiedenis
from .model import Bediening, Kwartier, Meting
from .planner import Planner, AutoStatus, mag_verkopen
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
        self.planner = Planner(cfg)
        self.planner.temp = self.voorsp.weer.op if hasattr(self.voorsp, "weer") else None
        from .licentie import Licentie
        self.licentie = Licentie(cfg)
        from .logboek import Logboek
        self.logboek = Logboek(data_map(cfg) / "logboek.db")
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
        self.maak_plan()
        self._taken = [asyncio.create_task(self._regel_lus()), asyncio.create_task(self._plan_lus()),
                       asyncio.create_task(self._data_lus()), asyncio.create_task(self._licentie_lus()),
                       asyncio.create_task(self._auto_lus())]

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
        if kw:  # het lopende kwartier is al deels voorbij
            deel = 1 - (nu - kw[0].start).total_seconds() / 900
            kw[0].zon_kwh *= deel
            kw[0].huis_kwh *= deel
        m = self.meting
        self._zet_autodoel(nu)
        auto = AutoStatus(verbonden=bool(m and m.auto_verbonden), geladen_kwh=self.auto_geladen(m))
        self.planner.ondergrens = self._omvormer_grens()
        self.plan = self.planner.maak(kw, m.accu_soc if m else 50.0, self.bed, auto, self.voorsp.wp_dag_kwh)
        log.info("Nieuw plan: %d kwartieren, verwachte besparing %s", len(kw), self.plan["samenvatting"].get("besparing"))

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
        gewenst = m.accu_kw - m.net_kw                      # + laden: net > 0 (afname) betekent minder laden / meer ontladen
        vorige = getattr(self, "_p1_vorige", 0.0)
        gewenst = vorige + 0.7 * (gewenst - vorige)         # dempen, anders slingert het
        if m.accu_soc >= float(a.get("max_soc", 100)) and gewenst > 0:
            gewenst = 0.0
        if m.accu_soc <= self.ondergrens() + 0.5 and gewenst < 0:
            gewenst = 0.0
        gewenst = max(-a["max_ontladen_kw"], min(a["max_laden_kw"], gewenst))
        kw = round(gewenst / 0.4) * 0.4                     # stapjes van 0,4 kW: niet elke paar seconden een nieuwe opdracht
        self._p1_vorige = gewenst
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
        if self.bed.accu != "auto":
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
            if k.accu_actie == "laden" and vraag < -0.02 and overschot_nu >= 0.8 * plan_kw:
                await self._eigen(m)
                b["accu"] = "Laadt met zonne-overschot"
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
                    b["accu"] = f"Verkoopt {self._k(kw - vraag_nu)} kW aan het net en voedt het huis (samen {self._k(kw)} kW)"
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
                    reden = "De zon levert meer dan het huis gebruikt; dat gaat de accu in in plaats van het net op."
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
            reeks.append((nu, m.net_kw, mk))

    def _meters_wijken_af(self) -> tuple[float, float] | None:
        """Wijken de meters al minstens twee minuten (aan metingen) meestal meer dan 1 kW af? Dan de gemiddelden, anders None."""
        reeks = list(self._meter_reeks)
        if len(reeks) < 12:
            return None
        af = [r for r in reeks if abs(r[1] - r[2]) > 1.0]
        if len(af) < 0.6 * len(reeks):
            return None
        return sum(r[1] for r in af) / len(af), sum(r[2] for r in af) / len(af)

    @staticmethod
    def _k(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    @staticmethod
    def _e(x: float) -> str:
        return f"€ {x:.2f}".replace(".", ",")

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
            await asyncio.sleep(self.cfg["regelaar"]["interval_s"])

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
                                  "(alle fasen), dan blijft de accu stil terwijl je huis van het net haalt.",
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
        return uit

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
                "prijsbron": self.prijzen.bron, "simulatie": bool(self.huis.sim),
                "apparaten": self.huis.onderdelen(), "fouten": self.huis.fouten, "meldingen": self.huis.meldingen,
                "storingen": self.storingen(), "tibber": {"actief": bool(self.prijzen.tibber), "fout": self.prijzen.tibber_fout},
                "accu_kwh": self.cfg["accu"]["capaciteit_kwh"], "max_afname_kw": self.cfg["net"]["max_afname_kw"],
                "plan_samenvatting": self.plan["samenvatting"], "licentie": self.licentie.status(),
                "auto": self.auto_overzicht(),
                "accu_temp": (getattr(self.huis.omvormer, "bms", None) or {}).get("temperatuur")}

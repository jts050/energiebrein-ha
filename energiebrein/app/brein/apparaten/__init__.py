"""Bundelt alle apparaten tot één 'huis' met één meting en vier knoppen: accu, zon, laadpaal, warmtepomp."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from ..model import Meting
from .simulator import SimHuis

log = logging.getLogger("huis")
TOLERANTIE_S = 120   # zo lang mag een apparaat zwijgen voordat het een storing is


def _getal(x) -> str:
    return f"{float(x):.1f}".replace(".", ",")


def _opdracht_tekst(naam: str, functie: str, args: tuple) -> str:
    """Opdracht aan een apparaat in gewone taal, voor het logboek."""
    if functie == "accu":
        actie, kw, doel = (list(args) + [0, 100])[:3]
        return {"laden": f"Thuisaccu: laden met {_getal(kw)} kW tot {float(doel):.0f}%",
                "ontladen": f"Thuisaccu: leveren met {_getal(kw)} kW",
                "vasthouden": "Thuisaccu: lading vasthouden",
                "zelfverbruik": "Thuisaccu: eigen verbruik (voedt het huis, laadt met zonne-overschot)"}.get(actie, f"Thuisaccu: {actie}")
    if functie == "exportlimiet":
        return "Zonnepanelen: terugleveren begrensd op 0 kW" if args and args[0] == 0 else "Zonnepanelen: terugleveren normaal"
    if functie == "laadstroom":
        a = args[0] if args else 0
        return "Laadpaal: laden gepauzeerd" if not a else f"Laadpaal: laadstroom {a:.0f} A"
    if functie == "warmtepomp":
        return {"boost": "Warmtepomp: extra stoken (goedkope stroom)", "wacht": "Warmtepomp: even wachten (dure stroom)",
                "normaal": "Warmtepomp: normaal"}.get(args[0] if args else "", f"Warmtepomp: {args}")
    return f"{naam}: {functie} {args}"


def _ip(x) -> str:
    return str(x or "").strip().partition("/")[0]


def dubbel_gebruik(cfg: dict) -> str | None:
    """Geeft een uitleg als de slimme meter (hoofdaansluiting) hetzelfde apparaat is als een ander onderdeel,
    bijvoorbeeld de Shelly die de warmtepomp meet. Dan meet de 'slimme meter' maar één groep en klopt niets meer."""
    p1 = (cfg.get("net") or {}).get("p1") or {}
    p1ip = _ip(p1.get("host"))
    if not p1ip or p1.get("type") in (None, "uit", "tibber"):
        return None
    anders = []
    wp = cfg.get("warmtepomp") or {}
    if wp.get("type") not in (None, "uit"):
        anders += [(wp.get("meter"), "de meter van de warmtepomp"), (wp.get("relais1"), "relais 1 van de warmtepomp"),
                   (wp.get("relais2"), "relais 2 van de warmtepomp")]
    lp = cfg.get("laadpaal") or {}
    if lp.get("type") not in (None, "uit"):
        anders.append((lp.get("host"), "de laadpaal"))
    omv = cfg.get("omvormer") or {}
    if omv.get("type") not in (None, "uit"):
        anders.append((omv.get("host"), "de omvormer"))
    for ip, rol in anders:
        if _ip(ip) == p1ip:
            return (f"Bij Slimme meter staat {p1ip}, maar dat apparaat is ook {rol}. De slimme meter moet het hele huis meten op "
                    "de hoofdaansluiting (meestal de HomeWizard P1 in de meterkast), niet één groep.")
    return None


class Huis:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sim = None
        self.omvormer = self.p1 = self.laadpaal = self.wp = None
        self.fouten: dict[str, str] = {}
        self.meldingen: dict[str, str] = {}   # geen fout, wel iets om te weten (bijv. 'alleen uitlezen')
        self.vergrendeld = False              # True = gratis versie: wel uitlezen, niets aansturen
        self.logboek = None                   # wordt door het brein gezet
        self._laatste_opdr: dict[str, tuple] = {}
        self._laatst: dict[str, tuple[datetime, dict]] = {}   # laatste goede meting per apparaat
        self._sinds_fout: dict[str, datetime] = {}
        if cfg.get("simulatie", True):
            self.sim = SimHuis(cfg)
            return
        from . import echt
        from . import merken as mk
        omv = {"goodwe": echt.GoodWe, "fronius": mk.FroniusGen24, "sunspec": echt.SunSpec, "solaredge": mk.SolarEdge,
               "sma": mk.SMA, "huawei": mk.Huawei, "victron": mk.Victron, "sungrow": mk.Sungrow, "sessy": mk.Sessy,
               "enphase": mk.Enphase, "deye": mk.DeyeSolarman, "solis": mk.Solis, "solis_cloud": mk.SolisCloud}.get(cfg["omvormer"].get("type"))
        if omv:
            self.omvormer = omv(cfg)
        p1 = {"homewizard": echt.HomeWizardP1, "youless": echt.Youless, "p1monitor": echt.P1Monitor,
              "shelly_em": echt.ShellyEM, "esphome": mk.ESPHomeP1, "tibber": mk.TibberPulse}.get(cfg["net"]["p1"].get("type"))
        if p1:
            self.p1 = p1(cfg)
        if cfg["laadpaal"].get("type") == "ocpp":
            self.laadpaal = echt.OCPPLaadpaal(cfg)
        elif cfg["laadpaal"].get("type") == "goe":
            self.laadpaal = echt.GoE(cfg)
        elif cfg["laadpaal"].get("type") == "easee":
            self.laadpaal = mk.Easee(cfg)
        elif cfg["laadpaal"].get("type") == "tesla":
            self.laadpaal = mk.TeslaWallConnector(cfg)
        if cfg["warmtepomp"].get("type") == "sgready_shelly":
            self.wp = echt.SGReadyShelly(cfg)
        elif cfg["warmtepomp"].get("type") == "meter":
            self.wp = echt.WPMeter(cfg)

    def onderdelen(self) -> dict:
        if self.sim:
            return {"omvormer": "Simulatie", "p1": "Simulatie", "laadpaal": "Simulatie" if self.cfg["laadpaal"].get("type") != "uit" else None,
                    "warmtepomp": "Simulatie" if self.cfg["warmtepomp"].get("type") != "uit" else None}
        namen = {"GoodWe": "GoodWe", "Fronius": "Fronius", "FroniusGen24": "Fronius", "SunSpec": "SunSpec (Modbus)", "SolarEdge": "SolarEdge",
                 "SMA": "SMA", "Huawei": "Huawei", "Victron": "Victron", "Sungrow": "Sungrow", "Sessy": "Sessy", "Enphase": "Enphase",
                 "DeyeSolarman": "Deye (Solarman)", "Solis": "Solis", "SolisCloud": "Solis (SolisCloud)", "ESPHomeP1": "ESPHome P1-lezer", "TibberPulse": "Tibber Pulse",
                 "TeslaWallConnector": "Tesla Wall Connector", "Easee": "Easee (via account)", "HomeWizardP1": "HomeWizard P1",
                 "Youless": "Youless", "P1Monitor": "P1 Monitor", "ShellyEM": "Shelly EM", "OCPPLaadpaal": "OCPP 1.6",
                 "GoE": "go-e Charger", "SGReadyShelly": "SG-ready"}
        n = lambda x: namen.get(type(x).__name__, type(x).__name__) if x else None
        return {"omvormer": n(self.omvormer), "p1": n(self.p1), "laadpaal": n(self.laadpaal), "warmtepomp": n(self.wp)}

    @property
    def kan_accu_sturen(self) -> bool:
        return bool(self.sim) or bool(self.omvormer and getattr(self.omvormer, "kan_sturen", False))

    @property
    def kan_vasthouden(self) -> bool:
        return bool(self.sim) or getattr(self.omvormer, "kan_vasthouden", True)

    async def start(self):
        for naam, d in self._alle():
            try:
                await asyncio.wait_for(d.start(), timeout=30)
            except Exception as e:
                self.fouten[naam] = str(e)
                log.error("%s start niet: %s", naam, e)

    async def stop(self):
        for _, d in self._alle():
            try:
                await d.stop()
            except Exception:
                pass

    def _alle(self):
        if self.sim:
            return [("simulatie", self.sim)]
        return [(n, d) for n, d in (("omvormer", self.omvormer), ("p1", self.p1), ("laadpaal", self.laadpaal), ("warmtepomp", self.wp)) if d]

    async def lees(self) -> Meting:
        if self.sim:
            return await self.sim.lees()
        m = Meting(tijd=datetime.now(timezone.utc))
        nu = m.tijd
        for naam, d in self._alle():
            try:
                waarden = await asyncio.wait_for(d.lees(), timeout=30)
                self._laatst[naam] = (nu, waarden)
                self._sinds_fout.pop(naam, None)
                self.fouten.pop(naam, None)
            except Exception as e:
                # Een gemiste meting is normaal bij wifi. Tot 2 minuten rekenen we door met de laatste goede waarden
                # en melden we niets; daarna pas een storing. De accu-lading blijft altijd de laatst bekende.
                eerste = self._sinds_fout.setdefault(naam, nu)
                tekst = str(e) or type(e).__name__
                log.warning("%s lezen mislukt: %s", naam, tekst)
                if (nu - eerste).total_seconds() > TOLERANTIE_S:
                    self.fouten[naam] = f"Al {int((nu - eerste).total_seconds() // 60)} minuten geen antwoord ({tekst})"
                vorige = self._laatst.get(naam)
                if not vorige:
                    continue
                oud = (nu - vorige[0]).total_seconds() > TOLERANTIE_S
                waarden = {k: v for k, v in vorige[1].items() if not oud or k == "accu_soc"}
            for k, v in waarden.items():
                if naam == "omvormer" and k == "net_kw" and self.p1:
                    continue  # P1-meter is leidend voor het net
                setattr(m, k, v)
        m.huis_kw = max(m.net_kw + m.zon_kw - m.accu_kw - m.auto_kw - m.wp_kw, 0.0)
        ct = getattr(self.omvormer, "meter_kw", None)
        if self.cfg["omvormer"].get("meter_meet", "net") == "huis" and ct is not None and self.p1:
            # De CT-meter van de omvormer meet het hele huis (niet de hoofdaansluiting). Dan is dat het echte huisverbruik,
            # en wat er aan zon bij komt bovenop de omvormer (andere zonne-omvormers) volgt uit de balans met de slimme meter.
            totaal = max(ct, 0.0)
            m.huis_kw = max(totaal - m.auto_kw - m.wp_kw, 0.0)
            m.zon_kw = max(m.zon_kw, totaal + m.accu_kw - m.net_kw)
        return m

    # ---- knoppen ----
    async def normaal(self):
        """Alles terug naar gewoon gedrag, zoals zonder Energiebrein: accu op zelfverbruik, geen exportlimiet,
        laadpaal op vol vermogen, warmtepomp normaal. Gebeurt eenmalig als Plus uitgaat."""
        slot, self.vergrendeld = self.vergrendeld, False
        try:
            await self.accu("zelfverbruik")
            await self.exportlimiet(None)
            if self.laadpaal:
                await self.laadstroom(self.cfg["laadpaal"].get("max_stroom_a", 16))
            await self.warmtepomp("normaal")
        finally:
            self.vergrendeld = slot

    async def _doe(self, naam, apparaat, functie, *args):
        if apparaat is None or (self.vergrendeld and not self.sim):
            return
        try:
            await getattr(apparaat, functie)(*args)
            self.fouten.pop(naam + "_opdracht", None)
            if self.logboek and self._laatste_opdr.get(naam + functie) != args:
                self._laatste_opdr[naam + functie] = args
                self.logboek.schrijf("opdracht", _opdracht_tekst(naam, functie, args), naam)
        except NotImplementedError as e:
            self.meldingen[naam] = str(e)
        except Exception as e:
            self.fouten[naam + "_opdracht"] = str(e)
            log.warning("%s opdracht mislukt: %s", naam, e)

    async def accu(self, actie, kw=0.0, doel=100.0):
        await self._doe("omvormer", self.sim or self.omvormer, "accu", actie, kw, doel)

    async def exportlimiet(self, kw):
        await self._doe("omvormer", self.sim or self.omvormer, "exportlimiet", kw)

    async def laadstroom(self, a):
        await self._doe("laadpaal", self.sim or self.laadpaal, "laadstroom", a)

    async def warmtepomp(self, stand):
        await self._doe("warmtepomp", self.sim or self.wp, "warmtepomp", stand)

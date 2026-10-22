"""Day-ahead beursprijzen (EPEX, Nederland) rechtstreeks ophalen — onafhankelijk van je energieleverancier.

Bronnen:
- ENTSO-E Transparency (officieel, gratis token)
- EnergyZero (open API, geen token)
- Model (als er geen internet is): een typisch Nederlands prijspatroon, zodat het systeem blijft werken.
"""
from __future__ import annotations
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aiohttp

from .net import sessie, uitleg

log = logging.getLogger("prijzen")
NL = "10YNL----------L"
KWARTIER = timedelta(minutes=15)


def kwartier_start(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def _vul_kwartieren(punten: dict[datetime, float], van: datetime, tot: datetime) -> dict[datetime, float]:
    """Zet uur- of kwartierprijzen om naar kwartieren; ontbrekende kwartieren krijgen de vorige prijs."""
    uit, laatste, t = {}, None, van
    while t < tot:
        if t in punten:
            laatste = punten[t]
        if laatste is not None:
            uit[t] = laatste
        t += KWARTIER
    return uit


async def _entsoe(token: str, van: datetime, tot: datetime) -> dict[datetime, float]:
    fmt = "%Y%m%d%H%M"
    params = {"securityToken": token, "documentType": "A44", "in_Domain": NL, "out_Domain": NL,
              "periodStart": van.strftime(fmt), "periodEnd": tot.strftime(fmt)}
    async with sessie() as s:
        async with s.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            xml = await r.text()
    root = ET.fromstring(xml)
    strip = lambda tag: re.sub(r"\{.*\}", "", tag)
    punten: dict[datetime, float] = {}
    for period in (e for e in root.iter() if strip(e.tag) == "Period"):
        start = res = None
        for e in period.iter():
            if strip(e.tag) == "start":
                start = datetime.strptime(e.text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            elif strip(e.tag) == "resolution":
                res = timedelta(minutes=15) if e.text == "PT15M" else timedelta(minutes=60)
        for p in (e for e in period if strip(e.tag) == "Point"):
            pos = int(next(c.text for c in p if strip(c.tag) == "position"))
            mwh = float(next(c.text for c in p if strip(c.tag) == "price.amount"))
            t0 = start + (pos - 1) * res
            for k in range(int(res / KWARTIER)):
                punten[t0 + k * KWARTIER] = mwh / 1000.0
    return punten


async def _energyzero(van: datetime, tot: datetime, aftrek: float) -> dict[datetime, float]:
    params = {"fromDate": van.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "tillDate": tot.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
              "interval": 4, "usageType": 1, "inclBtw": "false"}
    async with sessie() as s:
        async with s.get("https://api.energyzero.nl/v1/energyprices", params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            data = await r.json()
    punten = {}
    for p in data.get("Prices", []):
        t = datetime.fromisoformat(p["readingDate"].replace("Z", "+00:00"))
        for k in range(4):
            punten[t + k * KWARTIER] = float(p["price"]) - aftrek
    return punten


def model_prijs(t: datetime) -> float:
    """Typisch Nederlands patroon (excl. btw): goedkoop 's nachts en midden op de dag, duur rond 18-20 uur."""
    lokaal = t.astimezone()
    h = lokaal.hour + lokaal.minute / 60
    maand = lokaal.month
    zomer = math.cos((maand - 6.5) / 12 * 2 * math.pi) * -1  # +1 zomer, -1 winter
    basis = 0.09 - 0.015 * zomer
    avondpiek = 0.06 * math.exp(-((h - 19) / 2.0) ** 2)
    ochtendpiek = 0.03 * math.exp(-((h - 8) / 1.5) ** 2)
    zondal = (0.04 + 0.04 * max(zomer, 0)) * math.exp(-((h - 13.5) / 2.5) ** 2)
    nacht = -0.015 * math.exp(-((h - 3.5) / 2.0) ** 2)
    return round(basis + avondpiek + ochtendpiek - zondal + nacht, 5)


class Prijzen:
    def __init__(self, cfg: dict):
        self.cfg = cfg["prijzen"]
        self.beurs: dict[datetime, float] = {}
        self.bron = "model"
        self.bijgewerkt: datetime | None = None
        self.tibber: dict[datetime, float] = {}   # jouw echte Tibber-prijs per kwartier (alles inbegrepen)
        self.tibber_fout: str | None = None

    # --- wat jij betaalt en krijgt ---
    def inkoop(self, beurs: float, t: datetime | None = None) -> float:
        c = self.cfg
        if t is not None and t in self.tibber:
            return self.tibber[t]
        if c["contract"] == "vast":
            return c["vast_inkoop"]
        return (beurs + c["opslag"]) * (1 + c["btw"]) + c["energiebelasting"]

    def saldering_actief(self) -> bool:
        """Salderen mag in Nederland tot en met 31 december 2026. Instelling: auto (tot 2027 aan), aan of uit."""
        s = self.cfg.get("saldering", "auto")
        if s in (False, "uit"):
            return False
        if s in (True, "aan"):
            return True
        return datetime.now(timezone.utc).astimezone().date() < datetime(2027, 1, 1).date()

    def teruglever(self, beurs: float, t: datetime | None = None) -> float:
        """Wat een teruggeleverde kWh je oplevert. Met saldering streep je hem weg tegen een ingekochte kWh, dus krijg je
        er ook de energiebelasting en btw voor terug (zolang je over het jaar niet meer teruglevert dan je inkoopt)."""
        c = self.cfg
        if t is not None and t in self.tibber:
            # Met Tibber kennen we je echte prijs; daaruit halen we de kale beursprijs terug, zodat inkoop en
            # teruglevering bij elkaar passen (Tibber: totaal = (beurs + inkoopvergoeding + energiebelasting) × 1,21)
            eb_excl = c["energiebelasting"] / (1 + c["btw"])
            beurs = self.tibber[t] / (1 + c["btw"]) - c["opslag"] - eb_excl
        if c["contract"] == "vast":
            if self.saldering_actief():
                return c["vast_inkoop"]
            return c["vast_teruglever"]
        if self.saldering_actief():
            return beurs * (1 + c["btw"]) + c["energiebelasting"] - c["terugleverkosten"]
        return beurs - c["terugleverkosten"]

    async def ververs(self, nu: datetime | None = None) -> None:
        nu = (nu or datetime.now(timezone.utc)).astimezone(timezone.utc)
        van = nu.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=2)
        tot = van + timedelta(days=2, hours=2)
        punten, bron = {}, "model"
        try:
            if self.cfg.get("bron") == "entsoe" and self.cfg.get("entsoe_token"):
                punten, bron = await _entsoe(self.cfg["entsoe_token"], van, tot), "ENTSO-E"
            else:
                punten, bron = await _energyzero(van, tot, self.cfg.get("energyzero_aftrek", 0.0)), "EnergyZero"
        except Exception as e:  # geen internet of bron down: niet stoppen, verder met model
            log.warning("Prijzen ophalen mislukt (%s), ik reken met het modelpatroon", uitleg(e))
        if not punten:
            punten, bron = {}, "model"
            t = kwartier_start(nu) - timedelta(hours=1)
            eind = (nu.astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)).astimezone(timezone.utc)
            while t < eind:
                punten[t] = model_prijs(t)
                t += KWARTIER
        self.beurs = _vul_kwartieren(punten, min(punten), max(punten) + KWARTIER)
        self.bron, self.bijgewerkt = bron, nu
        if self.cfg.get("leverancier") == "tibber" and self.cfg.get("tibber_token"):
            await self._tibber()
        log.info("Prijzen: %d kwartieren van %s", len(self.beurs), bron)

    async def _tibber(self):
        """Officiële Tibber-API: je eigen prijs per kwartier, inclusief belastingen en Tibber-opslag."""
        veld = "today { total startsAt } tomorrow { total startsAt }"
        vragen = [f"{{ viewer {{ homes {{ currentSubscription {{ priceInfo(resolution: QUARTER_HOURLY) {{ {veld} }} }} }} }} }}",
                  f"{{ viewer {{ homes {{ currentSubscription {{ priceInfo {{ {veld} }} }} }} }} }}"]
        kop = {"Authorization": f"Bearer {self.cfg['tibber_token']}", "Content-Type": "application/json"}
        for vraag in vragen:
            try:
                async with sessie() as s:
                    async with s.post("https://api.tibber.com/v1-beta/gql", json={"query": vraag}, headers=kop,
                                      timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status in (401, 403):
                            self.tibber_fout = "Tibber-code klopt niet of is verlopen."
                            return
                        d = await r.json()
                if d.get("errors"):
                    continue
                punten = {}
                for home in d["data"]["viewer"]["homes"]:
                    pi = (home.get("currentSubscription") or {}).get("priceInfo") or {}
                    for p in (pi.get("today") or []) + (pi.get("tomorrow") or []):
                        punten[datetime.fromisoformat(p["startsAt"]).astimezone(timezone.utc)] = float(p["total"])
                    if punten:
                        break
                if punten:
                    self.tibber = _vul_kwartieren(punten, min(punten), max(punten) + timedelta(hours=1))
                    self.tibber_fout = None
                    self.bron += " + Tibber"
                    return
            except Exception as e:
                self.tibber_fout = f"Tibber niet bereikbaar: {uitleg(e)}"
        self.tibber_fout = self.tibber_fout or "Geen prijzen ontvangen van Tibber."

    def vanaf(self, t: datetime) -> list[tuple[datetime, float]]:
        t = kwartier_start(t.astimezone(timezone.utc))
        return sorted((k, v) for k, v in self.beurs.items() if k >= t)

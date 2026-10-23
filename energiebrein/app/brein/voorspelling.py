"""Voorspelt per kwartier: zonopbrengst en huisverbruik. Het huisverbruik leert van je eigen geschiedenis."""
from __future__ import annotations
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp

from .ai import HuisAI
from .config import data_map
from .geschiedenis import Geschiedenis
from .weer import Weer

log = logging.getLogger("voorspelling")

# Standaard dagprofiel huishouden (relatief, per uur) en seizoen — gebruikt tot er genoeg eigen data is
DAGPROFIEL = [0.55, 0.5, 0.48, 0.47, 0.48, 0.55, 0.75, 0.95, 0.9, 0.85, 0.85, 0.85,
              0.9, 0.85, 0.85, 0.9, 1.05, 1.35, 1.6, 1.55, 1.4, 1.25, 1.0, 0.75]
MAAND = [1.2, 1.12, 1.05, 0.95, 0.88, 0.82, 0.82, 0.85, 0.92, 1.02, 1.13, 1.22]
STOOK = [0.19, 0.16, 0.13, 0.08, 0.03, 0.01, 0.0, 0.0, 0.02, 0.07, 0.13, 0.18]
ZON_PER_KWP = [22, 40, 75, 110, 130, 135, 135, 115, 85, 55, 27, 17]  # kWh per maand
DAGLICHT = [(8.6, 16.8), (7.9, 17.7), (6.9, 18.6), (6.6, 20.4), (5.7, 21.3), (5.2, 22.0),
            (5.4, 21.9), (6.1, 21.0), (7.0, 19.8), (7.9, 18.6), (7.9, 16.9), (8.6, 16.5)]
MIN_DAGEN = 3


def _klok(t: datetime) -> tuple[datetime, float]:
    lokaal = t.astimezone()
    return lokaal, lokaal.hour + lokaal.minute / 60


def zon_model_kwh(t: datetime, kwp: float) -> float:
    """Gemiddelde zonopbrengst in een kwartier (geen weer). Alleen als er geen voorspelling binnenkomt."""
    lokaal, h = _klok(t)
    m = lokaal.month - 1
    op, onder = DAGLICHT[m]
    x = (h + 0.125 - op) / (onder - op)
    if not 0 < x < 1:
        return 0.0
    dag = ZON_PER_KWP[m] / 30 * kwp
    # integraal van sin^1.3 over de dag ≈ 0.58 * daglengte
    return dag * math.sin(math.pi * x) ** 1.3 / (0.58 * (onder - op)) * 0.25


class Voorspelling:
    def __init__(self, cfg: dict, geschiedenis: Geschiedenis):
        self.cfg = cfg
        self.gesch = geschiedenis
        self.zon_w: dict[datetime, float] = {}   # watt-tijdreeks van forecast.solar
        self.zon_bron = "model"
        self.zon_correctie = 1.0                   # leert hoe de voorspelling uitpakt op jouw dak
        self.kwp = sum(v["kwp"] for v in cfg.get("zonnepanelen", []))
        self._huisprofiel: dict[tuple[bool, int], float] = {}
        self._wp_per_dag: float | None = None
        self.leerdagen = 0
        self.bewaard_zon: dict[str, float] = {}    # kwartier-start (iso) → voorspelde kWh, om van te leren
        data = data_map(cfg)
        # wat het brein over je dak leerde, bewaard: anders begint het na elke herstart (bijv. instellingen opslaan) opnieuw
        self._zon_pad = data / "zon.json"
        try:
            import json
            z = json.loads(self._zon_pad.read_text())
            self.zon_correctie = max(0.6, min(1.4, float(z.get("correctie", 1.0))))
            self.bewaard_zon = {k: float(v) for k, v in (z.get("voorspeld") or {}).items()}
        except Exception:
            pass
        self.weer = Weer(cfg)
        self.ai_huis = HuisAI(data / "ai_huis.pkl", "huis_kwh", "huis")
        self.ai_wp = HuisAI(data / "ai_warmtepomp.pkl", "wp_kwh", "warmtepomp")
        self._ai_cache: dict[datetime, float] = {}
        self._afwijking = 0.0                       # hoeveel het huis nu afwijkt van de voorspelling (kWh/kwartier)
        self._afwijking_t: datetime | None = None
        self._laatst_getraind: datetime | None = None

    def bewaar(self, t: datetime, zon_kwh: float) -> None:
        self.bewaard_zon[t.astimezone(timezone.utc).isoformat()] = zon_kwh
        if len(self.bewaard_zon) > 4 * 24 * 10:
            for k in sorted(self.bewaard_zon)[: 4 * 24]:
                del self.bewaard_zon[k]

    async def ververs_zon(self) -> None:
        loc = self.cfg["locatie"]
        reeks: dict[datetime, float] = defaultdict(float)
        try:
            from .net import sessie
            async with sessie() as s:
                for vlak in self.cfg.get("zonnepanelen", []):
                    url = (f"https://api.forecast.solar/estimate/watts/{loc['breedte']}/{loc['lengte']}/"
                           f"{vlak['hoek']}/{vlak['richting']}/{vlak['kwp']}")
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        r.raise_for_status()
                        data = await r.json()
                    for ts, w in data["result"].items():
                        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone()  # lokale tijd
                        reeks[t.astimezone(timezone.utc)] += float(w)
            self.zon_w, self.zon_bron = dict(sorted(reeks.items())), "forecast.solar"
        except Exception as e:
            log.warning("Zonvoorspelling niet beschikbaar (%s), ik gebruik het gemiddelde", e)
            self.zon_bron = "model"

    def zon_kwh(self, t: datetime) -> float:
        if self.zon_bron == "forecast.solar" and self.zon_w:
            # gemiddelde van het vermogen aan begin en eind van het kwartier, lineair geïnterpoleerd
            w = (self._interp(t) + self._interp(t + timedelta(minutes=15))) / 2
            return w / 1000 * 0.25 * self.zon_correctie
        return zon_model_kwh(t, self.kwp) * self.zon_correctie

    def _interp(self, t: datetime) -> float:
        tijden = list(self.zon_w)
        if t <= tijden[0] or t >= tijden[-1]:
            return 0.0
        for a, b in zip(tijden, tijden[1:]):
            if a <= t <= b:
                fa, fb = self.zon_w[a], self.zon_w[b]
                return fa + (fb - fa) * (t - a) / (b - a)
        return 0.0

    # ---------- leren van eigen data ----------
    def leer(self, train: bool = False) -> None:
        nu = datetime.now(timezone.utc)
        if train or not self._laatst_getraind or nu - self._laatst_getraind > timedelta(hours=6):
            lang = self.gesch.rijen(nu - timedelta(days=60), nu)
            # leren mag nooit het brein stilleggen: lukt het niet, dan rekent het brein verder met het gemiddelde
            try:
                self.ai_huis.train(lang, self.weer)
            except Exception:
                log.exception("AI huis leren mislukt; ik gebruik het gemiddelde verbruik")
            if self.cfg.get("warmtepomp", {}).get("type", "uit") != "uit":
                try:
                    self.ai_wp.train(lang, self.weer)
                except Exception:
                    log.exception("AI warmtepomp leren mislukt")
            self._laatst_getraind = nu
        rijen = self.gesch.rijen(nu - timedelta(days=28), nu)
        per: dict[tuple[bool, int], list[tuple[float, float]]] = defaultdict(list)
        dagen, wp = set(), defaultdict(float)
        for r in rijen:
            if r["n"] < 3:
                continue
            t = datetime.fromisoformat(r["start"]).astimezone()
            leeftijd = (nu - t).total_seconds() / 86400
            sleutel = (t.weekday() >= 5, t.hour * 4 + t.minute // 15)
            per[sleutel].append((r["huis_kwh"], 0.93 ** leeftijd))   # recente dagen tellen zwaarder
            dagen.add(t.date())
            wp[t.date()] += r["wp_kwh"]
        self.leerdagen = len(dagen)
        if self.leerdagen >= MIN_DAGEN:
            self._huisprofiel = {k: sum(v * w for v, w in xs) / sum(w for _, w in xs) for k, xs in per.items() if xs}
            volle = [v for d, v in wp.items() if d != nu.astimezone().date()]
            self._wp_per_dag = sum(volle) / len(volle) if volle else None
        # zoncorrectie: verhouding gemeten / voorspeld over de kwartieren waarvoor we een voorspelling bewaarden
        gemeten = voorspeld = 0.0
        for r in rijen:
            v = self.bewaard_zon.get(r["start"])
            if v is not None and r["n"] >= 3:
                gemeten += r["zon_kwh"]
                voorspeld += v
        if voorspeld > 2.0:
            nieuw = max(0.6, min(1.4, gemeten / voorspeld * self.zon_correctie))
            self.zon_correctie = 0.7 * self.zon_correctie + 0.3 * nieuw
        try:
            import json
            grens = (nu - timedelta(days=10)).isoformat()
            self._zon_pad.write_text(json.dumps({"correctie": round(self.zon_correctie, 4),
                                                 "voorspeld": {k: round(v, 4) for k, v in self.bewaard_zon.items() if k >= grens}}))
        except Exception as e:
            log.warning("Zoncorrectie bewaren mislukt: %s", e)
        log.info("Geleerd van %d dagen", self.leerdagen)

    def bereid(self, tijden: list[datetime]) -> None:
        """Vooraf de AI-voorspelling voor het hele plan uitrekenen, en kijken hoe het huis er nu voor staat."""
        nu = datetime.now(timezone.utc)
        recent = [r for r in self.gesch.rijen(nu - timedelta(days=8), nu) if r["n"] >= 3]
        for r in recent:
            t = datetime.fromisoformat(r["start"])
            self.ai_huis.bijwerken(t, r["huis_kwh"])
            self.ai_wp.bijwerken(t, r["wp_kwh"])
        verleden = [datetime.fromisoformat(r["start"]).astimezone(timezone.utc) for r in recent[-5:-1]]
        alles = verleden + list(tijden)
        p = self.ai_huis.voorspel(alles, self.weer)
        self._ai_cache = dict(zip(alles, p)) if p is not None else {}
        # nu-correctie: wijkt het huis het afgelopen uur af (gasten, wasmachine, vakantie)? Dan dat de komende uren meenemen.
        if len(verleden) >= 2:
            fout = [r["huis_kwh"] - self.huis_basis(datetime.fromisoformat(r["start"])) for r in recent[-5:-1]]
            self._afwijking = sum(fout) / len(fout)
            self._afwijking_t = verleden[-1] + timedelta(minutes=15)
        else:
            self._afwijking, self._afwijking_t = 0.0, None

    def huis_kwh(self, t: datetime) -> float:
        basis = self.huis_basis(t)
        if self._afwijking_t is not None:
            stappen = max((t - self._afwijking_t).total_seconds() / 900, 0)
            basis += self._afwijking * 0.75 ** stappen   # dooft uit over een paar uur
        return max(basis, 0.0)

    def huis_basis(self, t: datetime) -> float:
        tu = t.astimezone(timezone.utc)
        if tu in self._ai_cache:
            return float(self._ai_cache[tu])
        lokaal = t.astimezone()
        sleutel = (lokaal.weekday() >= 5, lokaal.hour * 4 + lokaal.minute // 15)
        if sleutel in self._huisprofiel:
            return self._huisprofiel[sleutel]
        jaar = self.cfg["huis"]["jaarverbruik_kwh"]
        gem = sum(DAGPROFIEL) * sum(m * d for m, d in zip(MAAND, [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]))
        return jaar * DAGPROFIEL[lokaal.hour] * MAAND[lokaal.month - 1] / gem / 4

    def wp_dag_kwh(self, dag: datetime) -> float:
        wp = self.cfg.get("warmtepomp", {})
        if wp.get("type", "uit") == "uit":
            return 0.0
        if self.ai_wp.actief:
            begin = dag.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            p = self.ai_wp.voorspel([begin + timedelta(minutes=15 * k) for k in range(96)], self.weer)
            if p is not None:
                return float(p.sum())
        if self._wp_per_dag is not None:
            return self._wp_per_dag
        m = dag.astimezone().month - 1
        dagen = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m]
        return wp["jaarverbruik_kwh"] * STOOK[m] / dagen

    def status(self) -> dict:
        return {"zon_bron": self.zon_bron, "leerdagen": self.leerdagen,
                "geleerd": bool(self._huisprofiel) or self.ai_huis.actief,
                "zon_correctie": round(self.zon_correctie, 2), "weer_bron": self.weer.bron,
                "temp_nu": round(self.weer.op(datetime.now(timezone.utc)), 1),
                "afwijking_nu_kw": round(self._afwijking * 4, 2),
                "ai_huis": self.ai_huis.rapport, "ai_warmtepomp": self.ai_wp.rapport if self.cfg.get("warmtepomp", {}).get("type", "uit") != "uit" else None}

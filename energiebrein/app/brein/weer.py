"""Buitentemperatuur per uur (verleden én voorspelling) van Open-Meteo: gratis, geen account.
Zonder internet valt het terug op het normale Nederlandse temperatuurverloop voor die dag en dat uur."""
from __future__ import annotations
import logging
import math
import json
from datetime import datetime, timedelta, timezone

import aiohttp

from .config import data_map

log = logging.getLogger("weer")


def normaal(t: datetime) -> float:
    """Klimatologisch gemiddelde in Nederland (De Bilt-achtig): 3 °C half januari, 18 °C half juli, ±4 °C over de dag."""
    lokaal = t.astimezone()
    dag = lokaal.timetuple().tm_yday
    seizoen = 10.5 - 7.5 * math.cos((dag - 15) / 365 * 2 * math.pi)
    uur = lokaal.hour + lokaal.minute / 60
    return seizoen + 4.0 * math.cos((uur - 15) / 24 * 2 * math.pi)


ONWEER = {95, 96, 99}         # WMO-weercodes: onweer, onweer met hagel
WINDSTOOT_KMU = 75            # vanaf hier (code oranje-achtig) kan de stroom uitvallen


def stormuren(h: dict, nu: datetime) -> dict[datetime, str]:
    """Uren in de verwachting met onweer of zware windstoten."""
    uit = {}
    codes, stoten = h.get("weather_code") or [], h.get("wind_gusts_10m") or []
    for i, t in enumerate(h.get("time") or []):
        u = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        if u < nu - timedelta(hours=1):
            continue
        code = codes[i] if i < len(codes) else None
        stoot = stoten[i] if i < len(stoten) else None
        if code in ONWEER:
            uit[u] = "onweer"
        elif stoot is not None and stoot >= WINDSTOOT_KMU:
            uit[u] = f"zware windstoten ({stoot:.0f} km/u)"
    return uit


class Weer:
    def __init__(self, cfg: dict):
        self.loc = cfg["locatie"]
        self.temp: dict[datetime, float] = {}   # UTC uur → °C
        self.bron = "normaal"
        self.storm: dict[datetime, str] = {}     # UTC uur → "onweer" of "zware windstoten (90 km/u)", alleen de verwachting
        self.bestand = data_map(cfg) / "weer.json"         # bewaart gemeten temperaturen, zodat de AI er langer van kan leren
        try:
            self.temp = {datetime.fromisoformat(k): v for k, v in json.loads(self.bestand.read_text()).items()}
            if self.temp:
                self.bron = "opgeslagen"
        except Exception:
            pass

    async def ververs(self):
        params = {"latitude": self.loc["breedte"], "longitude": self.loc["lengte"], "hourly": "temperature_2m,weather_code,wind_gusts_10m",
                  "past_days": 60, "forecast_days": 3, "timezone": "UTC"}
        try:
            from .net import sessie
            async with sessie() as s:
                async with s.get("https://api.open-meteo.com/v1/forecast", params=params,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    r.raise_for_status()
                    d = await r.json()
            h = d["hourly"]
            self.temp.update({datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v
                              for t, v in zip(h["time"], h["temperature_2m"]) if v is not None})
            self.storm = stormuren(h, datetime.now(timezone.utc))
            grens = datetime.now(timezone.utc) - timedelta(days=400)
            self.temp = {k: v for k, v in self.temp.items() if k > grens}
            self.bron = "Open-Meteo"
            self.bewaar()
        except Exception as e:
            log.warning("Weer ophalen mislukt (%s), ik gebruik opgeslagen en normale temperaturen", e)
            self.bron = "opgeslagen" if self.temp else "normaal"

    def bewaar(self):
        try:
            self.bestand.write_text(json.dumps({k.isoformat(): v for k, v in sorted(self.temp.items())}))
        except Exception as e:
            log.warning("Weer opslaan mislukt: %s", e)

    def op(self, t: datetime) -> float:
        u = t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if u in self.temp:
            a, b = self.temp[u], self.temp.get(u + timedelta(hours=1), self.temp[u])
            return a + (b - a) * t.minute / 60
        return normaal(t)

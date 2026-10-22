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


class Weer:
    def __init__(self, cfg: dict):
        self.loc = cfg["locatie"]
        self.temp: dict[datetime, float] = {}   # UTC uur → °C
        self.bron = "normaal"
        self.bestand = data_map(cfg) / "weer.json"         # bewaart gemeten temperaturen, zodat de AI er langer van kan leren
        try:
            self.temp = {datetime.fromisoformat(k): v for k, v in json.loads(self.bestand.read_text()).items()}
            if self.temp:
                self.bron = "opgeslagen"
        except Exception:
            pass

    async def ververs(self):
        params = {"latitude": self.loc["breedte"], "longitude": self.loc["lengte"], "hourly": "temperature_2m",
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

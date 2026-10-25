"""Zonvoorspelling van Solcast (rooftop sites).

Een gratis Solcast-account (hobbyist) mag maar 10 keer per dag iets opvragen. Daarom: de laatste voorspelling wordt
bewaard (ook over een herstart heen) en pas na een paar uur ververst, en 's nachts niet."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("solcast")
URL = "https://api.solcast.com.au/rooftop_sites/{site}/forecasts?format=json&hours=48"


def sites(tekst: str) -> list[str]:
    """Resource-ID's uit wat de gebruiker invult: losse ID's of de hele API-link, gescheiden door komma's of spaties."""
    uit = []
    for deel in re.split(r"[,\s]+", tekst or ""):
        m = re.search(r"rooftop_sites/([0-9a-fA-F-]+)", deel)
        d = m.group(1) if m else deel.strip()
        if re.fullmatch(r"[0-9a-fA-F]{4}(-[0-9a-fA-F]{4}){3}|[0-9a-fA-F-]{8,}", d or "") and d not in uit:
            uit.append(d)
    return uit


def lees_antwoord(data: dict) -> dict[datetime, float]:
    """Solcast geeft per half uur het gemiddelde vermogen (kW), met het EINDE van dat half uur. Wij willen watt in het
    midden van het half uur, zodat de lijn tussen de punten klopt."""
    uit = {}
    for f in data.get("forecasts") or []:
        tekst = str(f["period_end"]).rstrip("Z").split(".")[0]          # 2026-10-25T11:30:00.0000000Z
        eind = datetime.fromisoformat(tekst)
        eind = eind.replace(tzinfo=timezone.utc) if eind.tzinfo is None else eind
        duur = 30
        m = re.fullmatch(r"PT(\d+)M", f.get("period") or "")
        if m:
            duur = int(m.group(1))
        uit[eind.astimezone(timezone.utc) - timedelta(minutes=duur / 2)] = float(f.get("pv_estimate") or 0.0) * 1000
    return uit


class Solcast:
    def __init__(self, cfg: dict, data: Path):
        self.cfg = cfg
        self.pad = data / "solcast.json"
        self.reeks: dict[datetime, float] = {}
        self.opgehaald: datetime | None = None
        self.fout: str | None = None
        self.vandaag_calls = (None, 0)
        try:
            z = json.loads(self.pad.read_text())
            self.reeks = {datetime.fromisoformat(k): v for k, v in z["reeks"].items()}
            self.opgehaald = datetime.fromisoformat(z["opgehaald"])
            self.vandaag_calls = (z.get("dag"), int(z.get("calls", 0)))
        except Exception:
            pass

    def instelling(self) -> tuple[str, list[str]]:
        z = self.cfg.get("zonvoorspelling") or {}
        return str(z.get("solcast_sleutel") or "").strip(), sites(str(z.get("solcast_sites") or ""))

    @property
    def aan(self) -> bool:
        sleutel, s = self.instelling()
        return bool(sleutel and s)

    def _mag_nu(self, nu: datetime) -> bool:
        """Zuinig met de 10 aanvragen per dag: om de paar uur, alleen overdag, en nooit meer dan 8 per dag."""
        _, s = self.instelling()
        dag = nu.astimezone().date().isoformat()
        calls = self.vandaag_calls[1] if self.vandaag_calls[0] == dag else 0
        if calls + len(s) > 8:
            return False
        if self.opgehaald is None:
            return True
        uren = 3 * max(len(s), 1)
        lokaal = nu.astimezone()
        if not (5 <= lokaal.hour < 21) and (nu - self.opgehaald) < timedelta(hours=12):
            return False
        return nu - self.opgehaald >= timedelta(hours=uren)

    async def ververs(self, sessie, forceer: bool = False) -> bool:
        """Haalt een nieuwe voorspelling op als dat mag. Geeft True als er een bruikbare (bewaarde of nieuwe) reeks is."""
        nu = datetime.now(timezone.utc)
        sleutel, s = self.instelling()
        if not (sleutel and s):
            return False
        if forceer or self._mag_nu(nu):
            import aiohttp
            reeks: dict[datetime, float] = {}
            try:
                for site in s:
                    async with sessie.get(URL.format(site=site), headers={"Authorization": f"Bearer {sleutel}"},
                                          timeout=aiohttp.ClientTimeout(total=25)) as r:
                        self._tel(nu)
                        if r.status == 429:
                            raise RuntimeError("Solcast: het maximum aantal aanvragen voor vandaag is bereikt")
                        if r.status in (401, 403):
                            raise RuntimeError("Solcast weigert de API-sleutel. Kijk hem na op toolkit.solcast.com.au → Account.")
                        if r.status == 404:
                            raise RuntimeError(f"Solcast kent de site {site} niet. Kijk het resource-ID na bij je rooftop site.")
                        r.raise_for_status()
                        for t, w in lees_antwoord(await r.json(content_type=None)).items():
                            reeks[t] = reeks.get(t, 0.0) + w
                if reeks:
                    self.reeks, self.opgehaald, self.fout = dict(sorted(reeks.items())), nu, None
                    self._bewaar()
            except Exception as e:
                self.fout = str(e)
                log.warning("Solcast ophalen mislukt: %s", e)
        # een bewaarde voorspelling is bruikbaar zolang hij nog een flink stuk vooruit kijkt
        return bool(self.reeks) and max(self.reeks) > nu + timedelta(hours=6)

    def _tel(self, nu: datetime):
        dag = nu.astimezone().date().isoformat()
        n = self.vandaag_calls[1] if self.vandaag_calls[0] == dag else 0
        self.vandaag_calls = (dag, n + 1)

    def _bewaar(self):
        try:
            self.pad.write_text(json.dumps({"opgehaald": self.opgehaald.isoformat(), "dag": self.vandaag_calls[0],
                                            "calls": self.vandaag_calls[1],
                                            "reeks": {k.isoformat(): round(v, 1) for k, v in self.reeks.items()}}))
        except Exception as e:
            log.warning("Solcast bewaren mislukt: %s", e)

    def status(self) -> dict:
        _, s = self.instelling()
        return {"aan": self.aan, "sites": len(s), "opgehaald": self.opgehaald.isoformat() if self.opgehaald else None,
                "fout": self.fout, "aanvragen_vandaag": self.vandaag_calls[1]}

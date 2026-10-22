"""De auto zelf koppelen via het account van de fabrikant: laadstand (%), actieradius en of de stekker erin zit.

Volkswagen, Audi, Skoda en Seat/Cupra lopen via de open-source bibliotheek CarConnectivity (dezelfde als de
Home Assistant-koppelingen voor deze merken). We vragen hooguit eens per 5 minuten de gegevens op: vaker
blokkeren de fabrikanten je account tijdelijk, en elke vraag kan de auto wakker maken.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("auto")

MERKEN = {"volkswagen": "Volkswagen (We Connect / VW-app)", "audi": "Audi (myAudi)", "skoda": "Skoda (MySkoda)",
          "seatcupra": "Seat / Cupra (My SEAT / My Cupra)"}
INTERVAL_S = 300


def _waarde(attr):
    try:
        return attr.value if attr is not None and attr.enabled else None
    except Exception:
        return None


def _naam(v) -> str:
    """Enum-waarde netjes als tekst."""
    if v is None:
        return ""
    return str(getattr(v, "value", v)).lower()


class AutoAccount:
    def __init__(self, cfg: dict, map_):
        c = cfg.get("auto") or {}
        self.merk = c.get("merk") or "uit"
        self.gebruiker = (c.get("gebruiker") or "").strip()
        self.wachtwoord = c.get("wachtwoord") or ""
        self.spin = (c.get("spin") or "").strip() or None
        self.vin = (c.get("vin") or "").strip().upper() or None
        self.map = map_
        self.cc = None
        self.gegevens: dict = {}
        self.fout: str | None = None
        self._laatst: datetime | None = None
        self._bezig = False

    @property
    def aan(self) -> bool:
        return self.merk in MERKEN and bool(self.gebruiker and self.wachtwoord)

    # ---------------------------------------------------------------- intern (draait in een aparte thread)
    def _maak(self):
        from carconnectivity import carconnectivity
        conf = {"username": self.gebruiker, "password": self.wachtwoord, "interval": INTERVAL_S}
        if self.spin:
            conf["spin"] = self.spin
        config = {"carConnectivity": {"log_level": "warning", "connectors": [{"type": self.merk, "config": conf}]}}
        return carconnectivity.CarConnectivity(config=config, tokenstore_file=str(self.map / f"auto_{self.merk}.token"))

    def _haal(self) -> dict:
        if self.cc is None:
            self.cc = self._maak()
        self.cc.fetch_all()
        try:
            self.cc.persist()
        except Exception:
            pass
        garage = self.cc.get_garage()
        autos = garage.list_vehicles() if garage else []
        if not autos:
            raise RuntimeError("er staat geen auto op dit account")
        auto = next((a for a in autos if self.vin and _waarde(a.vin) == self.vin), autos[0])
        soc = bereik = None
        for drive in (auto.drives.drives.values() if getattr(auto, "drives", None) else []):
            if _naam(_waarde(drive.type)) in ("electric", "") or soc is None:
                soc = _waarde(getattr(drive, "level", None)) if soc is None else soc
                bereik = _waarde(getattr(drive, "range", None)) if bereik is None else bereik
        laden = getattr(auto, "charging", None)
        staat = _naam(_waarde(laden.state)) if laden else ""
        stekker = _naam(_waarde(laden.connector.connection_state)) if laden and getattr(laden, "connector", None) else ""
        doel = _waarde(laden.settings.target_level) if laden and getattr(laden, "settings", None) else None
        return {"naam": " ".join(x for x in (_waarde(auto.manufacturer), _waarde(auto.model)) if x) or _waarde(auto.name) or "Je auto",
                "vin": _waarde(auto.vin), "soc": None if soc is None else float(soc), "bereik_km": None if bereik is None else round(float(bereik)),
                "aangesloten": stekker == "connected" if stekker else None, "laadt": staat == "charging",
                "laadstand": staat, "doel_soc_auto": None if doel is None else float(doel),
                "bijgewerkt": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---------------------------------------------------------------- voor het brein
    async def ververs(self, forceer: bool = False) -> dict:
        if not self.aan or self._bezig:
            return self.gegevens
        nu = datetime.now(timezone.utc)
        if not forceer and self._laatst and (nu - self._laatst).total_seconds() < INTERVAL_S:
            return self.gegevens
        self._bezig, self._laatst = True, nu
        try:
            self.gegevens = await asyncio.wait_for(asyncio.to_thread(self._haal), timeout=120)
            self.fout = None
            log.info("Auto: %s %s%%, %s km", self.gegevens.get("naam"), self.gegevens.get("soc"), self.gegevens.get("bereik_km"))
        except ImportError:
            self.fout = "de koppeling met automerken is nog niet geïnstalleerd (pip install carconnectivity-connector-" + self.merk + ")"
        except Exception as e:
            ruw = (str(e) or type(e).__name__).strip()
            l = ruw.lower()
            merk = MERKEN[self.merk].split(" (")[0]
            if "consent" in l or "terms" in l or "legal" in l or "marketing" in l:
                tekst = (f"je moet eerst nieuwe voorwaarden accepteren: open de app van {merk} op je telefoon, log uit en weer in "
                         "en accepteer wat hij vraagt")
            elif "throttl" in l or "too many" in l or "429" in l:
                tekst = f"{merk} blokkeert even, door te vaak inloggen. Wacht een uur en druk dan pas weer op Test"
            elif "spin" in l:
                tekst = "de S-PIN klopt niet of is nodig; vul de S-PIN uit de app van je auto in"
            elif any(w in l for w in ("auth", "password", "login", "credential", "401", "403")):
                tekst = (f"inloggen bij {merk} lukt niet. Klopt het wachtwoord, open dan de app van je auto en kijk of hij "
                         "nieuwe voorwaarden wil laten accepteren")
            else:
                tekst = f"uitlezen bij {merk} lukt niet"
            tekst += f" (melding van {merk}: {ruw[:180]})"
            # een oude of halve inlog weggooien, zodat de volgende poging schoon begint
            self.stop()
            self.cc = None
            try:
                (self.map / f"auto_{self.merk}.token").unlink(missing_ok=True)
            except Exception:
                pass
            self.fout = tekst
            log.warning("Auto uitlezen mislukt: %s", tekst)
        finally:
            self._bezig = False
        return self.gegevens

    def stop(self):
        try:
            if self.cc:
                self.cc.shutdown()
        except Exception:
            pass

    def status(self) -> dict:
        return {"aan": self.aan, "merk": self.merk, "merk_naam": MERKEN.get(self.merk, ""), "fout": self.fout, **self.gegevens}

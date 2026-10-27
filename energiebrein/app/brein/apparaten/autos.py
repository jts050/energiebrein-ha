"""De auto zelf koppelen via het account van de fabrikant: laadstand (%), actieradius en of de stekker erin zit.

Volkswagen, Audi, Skoda en Seat/Cupra lopen via de open-source bibliotheek CarConnectivity (dezelfde als de
Home Assistant-koppelingen voor deze merken). We vragen hooguit eens per 5 minuten de gegevens op: vaker
blokkeren de fabrikanten je account tijdelijk, en elke vraag kan de auto wakker maken.

Sinds eind mei 2026 laat Volkswagen geen andere programma's meer inloggen via de VW-app (We Connect). De route die
wel werkt is het officiële EU Data Act-portaal van de VW-groep: alleen uitlezen, ongeveer elke 15 minuten nieuwe
gegevens. Daarvoor zet je één keer in het portaal een doorlopende gegevensaanvraag aan.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("auto")

MERKEN = {"eu_volkswagen": "Volkswagen (EU Data Act-portaal)", "eu_audi": "Audi (EU Data Act-portaal)",
          "eu_skoda": "Skoda (EU Data Act-portaal)", "eu_seat": "Seat (EU Data Act-portaal)", "eu_cupra": "Cupra (EU Data Act-portaal)",
          "volkswagen": "Volkswagen (We Connect / VW-app)", "audi": "Audi (myAudi)", "skoda": "Skoda (MySkoda)",
          "seatcupra": "Seat / Cupra (My SEAT / My Cupra)"}
EU_MERK = {"eu_volkswagen": "VOLKSWAGEN_PASSENGER_CARS", "eu_audi": "AUDI", "eu_skoda": "SKODA", "eu_seat": "SEAT", "eu_cupra": "CUPRA"}
EU_PORTAAL = "https://eu-data-act.drivesomethinggreater.com/"
INTERVAL_S = 300
INTERVAL_EU_S = 900          # het portaal maakt maar elke 15 minuten nieuwe gegevens


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
    @property
    def eu(self) -> bool:
        return self.merk in EU_MERK

    @property
    def interval_s(self) -> int:
        return INTERVAL_EU_S if self.eu else INTERVAL_S

    def _maak(self):
        from carconnectivity import carconnectivity
        if self.eu:
            conf = {"username": self.gebruiker, "password": self.wachtwoord, "interval": INTERVAL_EU_S,
                    "country": "nl", "language": "nl", "brand": EU_MERK[self.merk]}
            if self.vin:
                conf["vin"] = self.vin
            soort = "vw_eu_data_act"
        else:
            conf = {"username": self.gebruiker, "password": self.wachtwoord, "interval": INTERVAL_S}
            if self.spin:
                conf["spin"] = self.spin
            soort = self.merk
        config = {"carConnectivity": {"log_level": "warning", "connectors": [{"type": soort, "config": conf}]}}
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
            if self.eu:
                raise RuntimeError("geen_gegevens: het portaal heeft nog geen gegevens van je auto")
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
        if not forceer and self._laatst and (nu - self._laatst).total_seconds() < self.interval_s:
            return self.gegevens
        self._bezig, self._laatst = True, nu
        try:
            self.gegevens = await asyncio.wait_for(asyncio.to_thread(self._haal), timeout=120)
            self.fout = None
            log.info("Auto: %s %s%%, %s km", self.gegevens.get("naam"), self.gegevens.get("soc"), self.gegevens.get("bereik_km"))
        except ImportError:
            pakket = "vw-eu-data-act" if self.eu else self.merk
            self.fout = "de koppeling met automerken is nog niet geïnstalleerd (pip install carconnectivity-connector-" + pakket + ")"
        except Exception as e:
            ruw = (str(e) or type(e).__name__).strip()
            l = ruw.lower()
            merk = MERKEN[self.merk].split(" (")[0]
            if self.eu and ("geen_gegevens" in l or "no_content" in l or "no_data" in l or "dataset" in l):
                tekst = (f"het EU Data Act-portaal heeft nog geen gegevens van je {merk}. Log in op {EU_PORTAAL}, ga naar "
                         "Data clusters → Vehicle overview, koppel je auto en zet bij 'Get customised data' een doorlopende "
                         "aanvraag van 15 minuten aan. Het eerste bestand kan even duren")
            elif self.eu and ("consent" in l or "terms" in l or "legal" in l):
                tekst = (f"het portaal wil dat je eerst de voorwaarden accepteert. Log één keer in op {EU_PORTAAL} met hetzelfde "
                         "account en accepteer wat het portaal vraagt")
            elif self.merk == "volkswagen" and ("weconnect" in l or "authorization url" in l or "403" in l):
                tekst = ("Volkswagen laat sinds eind mei 2026 geen andere programma's meer inloggen via de VW-app. Kies bij Merk "
                         "'Volkswagen (EU Data Act-portaal)': dat is de officiële route die wel werkt")
            elif "consent" in l or "terms" in l or "legal" in l or "marketing" in l:
                tekst = (f"je moet eerst nieuwe voorwaarden accepteren: open de app van {merk} op je telefoon, log uit en weer in "
                         "en accepteer wat hij vraagt")
            elif "throttl" in l or "too many" in l or "429" in l:
                tekst = f"{merk} blokkeert even, door te vaak inloggen. Wacht een uur en druk dan pas weer op Test"
            elif "spin" in l:
                tekst = "de S-PIN klopt niet of is nodig; vul de S-PIN uit de app van je auto in"
            elif self.eu and any(w in l for w in ("auth", "password", "login", "credential", "401", "403")):
                tekst = (f"inloggen bij het EU Data Act-portaal lukt niet. Controleer je e-mail en wachtwoord (hetzelfde account "
                         f"als de app van je {merk}) en log één keer in op {EU_PORTAAL} om te kijken of het portaal iets vraagt")
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

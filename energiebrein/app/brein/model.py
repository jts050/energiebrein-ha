"""Gedeelde datatypes. Vermogens in kW, energie in kWh. Netvermogen: + = afname, - = teruglevering."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime

KWARTIER_H = 0.25


@dataclass
class Meting:
    tijd: datetime
    net_kw: float = 0.0          # P1: + afname, - teruglevering
    zon_kw: float = 0.0
    accu_kw: float = 0.0         # + laden, - ontladen
    accu_soc: float = 0.0        # %
    auto_kw: float = 0.0
    auto_verbonden: bool = False
    auto_geladen_kwh: float = 0.0   # in deze laadsessie
    wp_kw: float = 0.0
    wp_stand: str = "normaal"    # sg-ready stand
    huis_kw: float = 0.0         # rest van het huis (berekend)

    def dict(self):
        d = asdict(self)
        d["tijd"] = self.tijd.isoformat()
        return d


@dataclass
class Kwartier:
    start: datetime
    beurs: float                 # €/kWh excl. btw
    inkoop: float                # €/kWh wat jij betaalt
    teruglever: float            # €/kWh wat jij krijgt
    zon_kwh: float = 0.0         # voorspeld
    huis_kwh: float = 0.0        # voorspeld (zonder auto en warmtepomp)
    wp_kwh: float = 0.0          # gepland
    auto_kwh: float = 0.0        # gepland
    accu_kwh: float = 0.0        # gepland, + laden / - ontladen (aan accu-kant)
    soc_na: float = 0.0          # % na dit kwartier
    net_kwh: float = 0.0         # verwacht
    wp_stand: str = "normaal"    # normaal / boost / wacht
    aftoppen: bool = False
    waarde_kwh: float = 0.0      # wat een opgeslagen kWh later in het plan waard is (€)
    reden: str = ""

    @property
    def accu_actie(self) -> str:
        if self.accu_kwh > 0.02:
            return "laden"
        if self.accu_kwh < -0.02:
            return "ontladen"
        return "rust"

    def dict(self):
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["accu_actie"] = self.accu_actie
        return d


@dataclass
class Bediening:
    """Wat de gebruiker in de app kiest. 'auto' = het brein beslist."""
    accu: str = "auto"           # auto / laden / ontladen / vasthouden / zelfverbruik
    laadpaal: str = "auto"       # auto / zon / nu / stop
    auto_doel_kwh: float = 20.0
    auto_vertrek: str = "07:30"
    auto_deadline: str | None = None   # volgende vertrektijd (UTC, iso) uit het autoplan; None = geen vertrek gepland
    auto_zonder: str = "zon"           # zonder vertrek: zon (alleen overschot) of goedkoop
    warmtepomp: str = "auto"     # auto / boost / normaal / wacht
    zon: str = "auto"            # auto (aftoppen bij negatieve prijs) / altijd

    def dict(self):
        return asdict(self)

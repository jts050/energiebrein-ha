"""Licentie: mag dit kastje automatisch sturen (Plus) of alleen meten, plannen en adviseren (gratis)?

- Geen licentieserver ingesteld (config licentie.server leeg) = eigen gebruik: alles werkt.
- Wel ingesteld: het kastje meldt zich bij de start en elke 6 uur. Valt het internet weg, dan blijft
  Plus nog 14 dagen werken na de laatste geslaagde controle, zolang de einddatum niet voorbij is.
- In de simulatie wordt nooit iets echt aangestuurd, dus daar is alles altijd te proberen.
"""
from __future__ import annotations
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from .config import DATA

log = logging.getLogger("licentie")
VERSIE = "1.0"
OFFLINE_DAGEN = 14
BESTAND = DATA / "licentie.json"


def kastje_id() -> str:
    """Vaste, willekeurige code voor dit kastje (geen persoonsgegevens)."""
    pad = DATA / "kastje_id"
    try:
        return pad.read_text().strip()
    except Exception:
        pad.parent.mkdir(parents=True, exist_ok=True)
        kid = uuid.uuid4().hex
        pad.write_text(kid)
        return kid


class Licentie:
    def __init__(self, cfg: dict):
        c = cfg.get("licentie") or {}
        self.server = (c.get("server") or "").rstrip("/")
        self.sleutel = (c.get("sleutel") or "").strip().upper()
        self.cfg = cfg
        self.kastje = kastje_id()
        self.antwoord: dict = {}
        self.laatst_ok: datetime | None = None
        self.fout: str | None = None
        try:
            d = json.loads(BESTAND.read_text())
            if d.get("sleutel") == self.sleutel and d.get("server") == self.server:
                self.antwoord = d.get("antwoord") or {}
                self.laatst_ok = datetime.fromisoformat(d["laatst_ok"]) if d.get("laatst_ok") else None
        except Exception:
            pass

    # ---------------------------------------------------------------- oordeel
    @property
    def eigen_gebruik(self) -> bool:
        return not self.server

    @property
    def plus(self) -> bool:
        if self.eigen_gebruik:
            return True
        if self.antwoord.get("plan") != "plus" or not self.laatst_ok:
            return False
        tot = self.antwoord.get("geldig_tot")
        if tot and date.fromisoformat(tot) < date.today():
            return False
        return datetime.now(timezone.utc) - self.laatst_ok < timedelta(days=OFFLINE_DAGEN)

    def status(self) -> dict:
        if self.eigen_gebruik:
            return {"plan": "plus", "eigen_gebruik": True, "tekst": "Eigen gebruik: alle functies staan aan.", "kastje": self.kastje[:8]}
        tekst = self.antwoord.get("tekst") or ("Nog niet gecontroleerd." if self.sleutel else "Gratis versie: meten, plannen en advies.")
        if self.antwoord.get("plan") == "plus" and not self.plus:
            tekst = (f"Plus staat uit: het kastje kon de licentie al meer dan {OFFLINE_DAGEN} dagen niet controleren. Controleer de internetverbinding."
                     if self.laatst_ok else tekst)
        elif self.plus and self.fout:
            over = OFFLINE_DAGEN - (datetime.now(timezone.utc) - self.laatst_ok).days
            tekst += f" Controle lukt nu niet ({self.fout}); Plus blijft nog {over} dagen werken."
        return {"plan": "plus" if self.plus else "gratis", "eigen_gebruik": False, "tekst": tekst,
                "sleutel": self.sleutel, "geldig_tot": self.antwoord.get("geldig_tot"), "klant": self.antwoord.get("klant"),
                "laatst_gecontroleerd": self.laatst_ok.isoformat() if self.laatst_ok else None, "fout": self.fout, "kastje": self.kastje[:8]}

    # ---------------------------------------------------------------- controleren
    async def controleer(self, apparaten: dict | None = None) -> bool:
        """Vraagt de licentieserver wat dit kastje mag. Geeft True als de controle lukte."""
        if self.eigen_gebruik:
            return True
        from .net import sessie, uitleg
        try:
            async with sessie(20) as s:
                async with s.post(self.server + "/api/v1/melding", json={
                        "sleutel": self.sleutel, "kastje_id": self.kastje, "versie": VERSIE,
                        "simulatie": bool(self.cfg.get("simulatie", True)), "apparaten": apparaten or {}}) as r:
                    r.raise_for_status()
                    d = await r.json(content_type=None)
            if "plan" not in d:
                raise ValueError("onverwacht antwoord van de licentieserver")
            self.antwoord, self.laatst_ok, self.fout = d, datetime.now(timezone.utc), None
            self._bewaar()
            log.info("Licentie: %s", d.get("tekst"))
            return True
        except Exception as e:
            self.fout = uitleg(e)
            log.warning("Licentie controleren mislukt: %s", self.fout)
            return False

    def _bewaar(self):
        try:
            BESTAND.parent.mkdir(parents=True, exist_ok=True)
            BESTAND.write_text(json.dumps({"server": self.server, "sleutel": self.sleutel, "antwoord": self.antwoord,
                                           "laatst_ok": self.laatst_ok.isoformat() if self.laatst_ok else None}))
        except Exception as e:
            log.warning("Licentie opslaan mislukt: %s", e)

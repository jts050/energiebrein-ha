"""Logboek: wat het brein besluit, welke opdrachten naar je apparaten gaan, storingen die komen en gaan,
en wat je zelf wijzigt. Bewaard in een eigen database naast de metingen, 120 dagen lang."""
from __future__ import annotations
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("logboek")
SOORTEN = {"besluit", "opdracht", "storing", "opgelost", "handmatig", "systeem"}
BEWAAR_DAGEN = 120


def kern(tekst: str) -> str:
    """Tekst zonder getallen en bedragen: zo zien we of een besluit écht anders is, of alleen de prijs veranderde."""
    t = re.sub(r"\([^)]*\)", "", tekst or "")
    return re.sub(r"[€\d.,%:]+", "", t).strip().lower()


class Logboek:
    def __init__(self, pad: Path):
        self.db = sqlite3.connect(pad, check_same_thread=False)
        self.slot = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS logboek (id INTEGER PRIMARY KEY AUTOINCREMENT, tijd TEXT NOT NULL, "
                        "soort TEXT NOT NULL, apparaat TEXT, tekst TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS logboek_tijd ON logboek(tijd)")
        self.db.commit()
        self._laatst: dict[str, str] = {}
        self._opruimen()

    def schrijf(self, soort: str, tekst: str, apparaat: str | None = None):
        if soort not in SOORTEN or not tekst:
            return
        try:
            with self.slot:
                self.db.execute("INSERT INTO logboek (tijd, soort, apparaat, tekst) VALUES (?, ?, ?, ?)",
                                (datetime.now(timezone.utc).isoformat(timespec="seconds"), soort, apparaat, tekst[:500]))
                self.db.commit()
        except Exception as e:
            log.warning("Logboek schrijven mislukt: %s", e)

    def als_anders(self, sleutel: str, soort: str, tekst: str, apparaat: str | None = None):
        """Alleen opschrijven als het wezenlijk anders is dan de vorige keer voor deze sleutel."""
        k = kern(tekst)
        if self._laatst.get(sleutel) == k:
            return
        self._laatst[sleutel] = k
        self.schrijf(soort, tekst, apparaat)

    def rijen(self, dagen: int = 7, soort: str | None = None, limiet: int = 500) -> list[dict]:
        grens = (datetime.now(timezone.utc) - timedelta(days=dagen)).isoformat()
        sql, args = "SELECT tijd, soort, apparaat, tekst FROM logboek WHERE tijd >= ?", [grens]
        if soort == "storing":
            sql += " AND soort IN ('storing', 'opgelost')"
        elif soort:
            sql += " AND soort = ?"
            args.append(soort)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limiet)
        with self.slot:
            return [dict(zip(("tijd", "soort", "apparaat", "tekst"), r)) for r in self.db.execute(sql, args)]

    def _opruimen(self):
        grens = (datetime.now(timezone.utc) - timedelta(days=BEWAAR_DAGEN)).isoformat()
        with self.slot:
            self.db.execute("DELETE FROM logboek WHERE tijd < ?", (grens,))
            self.db.commit()

"""Slaat per kwartier op wat er echt gebeurde. Hiermee leert het brein je huis kennen."""
from __future__ import annotations
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import Meting, KWARTIER_H

VELDEN = ["net_kwh", "zon_kwh", "accu_kwh", "auto_kwh", "wp_kwh", "huis_kwh", "kosten", "kosten_zonder", "soc", "beurs",
          "afname_kwh", "terug_kwh"]


class Geschiedenis:
    def __init__(self, pad: Path):
        self.db = sqlite3.connect(pad, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS kwartier (start TEXT PRIMARY KEY, n INTEGER)")
        bestaand = {r[1] for r in self.db.execute("PRAGMA table_info(kwartier)")}
        for v in VELDEN:   # oudere databases bijwerken
            if v not in bestaand:
                self.db.execute(f"ALTER TABLE kwartier ADD COLUMN {v} REAL")
        self.db.commit()
        self._lopend: dict | None = None

    def meet(self, m: Meting, inkoop: float, teruglever: float, interval_s: float, beurs: float | None = None) -> None:
        """Telt een meting op bij het lopende kwartier."""
        t = m.tijd.astimezone(timezone.utc)
        start = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
        h = interval_s / 3600
        if self._lopend and self._lopend["start"] != start:
            self._schrijf(self._lopend)
            self._lopend = None
        if not self._lopend:
            self._lopend = {"start": start, **{v: 0.0 for v in VELDEN}, "n": 0}
        r = self._lopend
        net = m.net_kw * h
        zonder = (m.net_kw - m.accu_kw) * h  # wat het net was geweest zonder accu
        r["net_kwh"] += net
        r["zon_kwh"] += m.zon_kw * h
        r["accu_kwh"] += m.accu_kw * h
        r["auto_kwh"] += m.auto_kw * h
        r["wp_kwh"] += m.wp_kw * h
        r["huis_kwh"] += m.huis_kw * h
        r["kosten"] += net * (inkoop if net > 0 else teruglever)
        r["kosten_zonder"] += zonder * (inkoop if zonder > 0 else teruglever)
        r["afname_kwh"] += max(net, 0.0)
        r["terug_kwh"] += max(-net, 0.0)
        r["soc"] = m.accu_soc
        r["beurs"] = beurs
        r["n"] += 1

    def _schrijf(self, r: dict) -> None:
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO kwartier (start, n, {', '.join(VELDEN)}) VALUES (?, ?, {', '.join('?' for _ in VELDEN)})",
                            [r["start"].isoformat(), r["n"]] + [r.get(v) for v in VELDEN])
            self.db.commit()

    def rijen(self, van: datetime, tot: datetime | None = None) -> list[dict]:
        tot = tot or datetime.now(timezone.utc) + timedelta(days=1)
        with self.lock:
            cur = self.db.execute("SELECT * FROM kwartier WHERE start >= ? AND start < ? ORDER BY start",
                                  (van.astimezone(timezone.utc).isoformat(), tot.astimezone(timezone.utc).isoformat()))
            namen = [c[0] for c in cur.description]
            uit = [dict(zip(namen, r)) for r in cur.fetchall()]
        if self._lopend and van <= self._lopend["start"] < tot:
            uit.append({**self._lopend, "start": self._lopend["start"].isoformat()})
        return uit

    def soc_voor(self, t: datetime) -> float | None:
        """Laadtoestand van de accu aan het eind van het laatste kwartier vóór t (binnen 6 uur), als die bekend is."""
        with self.lock:
            r = self.db.execute("SELECT soc FROM kwartier WHERE start < ? AND start >= ? AND soc IS NOT NULL ORDER BY start DESC LIMIT 1",
                                (t.astimezone(timezone.utc).isoformat(), (t - timedelta(hours=6)).astimezone(timezone.utc).isoformat())).fetchone()
        return float(r[0]) if r else None

    def vandaag(self) -> dict:
        nu = datetime.now().astimezone()
        middernacht = nu.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.rijen(middernacht)
        som = lambda k: sum(r[k] or 0 for r in rows)
        begin = self.soc_voor(middernacht)
        if begin is None and rows and rows[0].get("soc") is not None:
            begin = float(rows[0]["soc"])
        return {"kosten": som("kosten"), "kosten_zonder_accu": som("kosten_zonder"),
                "zon_kwh": som("zon_kwh"), "afname_kwh": sum(max(r["net_kwh"] or 0, 0) for r in rows),
                "teruglevering_kwh": -sum(min(r["net_kwh"] or 0, 0) for r in rows), "kwartieren": len(rows),
                "soc_begin": begin}

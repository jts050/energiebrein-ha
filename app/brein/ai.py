"""AI die je huis leert kennen.

Een gradient-boosting-model (scikit-learn) leert je verbruik per kwartier uit:
  tijdstip van de dag · weekdag · buitentemperatuur · verbruik precies een week eerder · je verbruiksniveau van de afgelopen week.

Het model controleert zichzelf: het traint op alles behalve de laatste 2 dagen, voorspelt die 2 dagen,
en vergelijkt dat met het simpele gemiddelde per kwartier. Het brein gebruikt de AI alleen als die beter scoort.
"""
from __future__ import annotations
import logging
import math
import pickle
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger("ai")
MIN_DAGEN = 7
TEST_DAGEN = 2
KW = timedelta(minutes=15)


def _kwartier(t: datetime) -> int:
    lokaal = t.astimezone()
    return lokaal.hour * 4 + lokaal.minute // 15


class HuisAI:
    def __init__(self, pad: Path, doel: str = "huis_kwh", naam: str = "huis"):
        self.pad, self.doel, self.naam = pad, doel, naam
        self.model = None
        self.actief = False
        self.rapport: dict = {"status": "verzamelt data", "dagen": 0}
        self._reeks: dict[datetime, float] = {}   # UTC kwartier → gemeten kWh (voor 'week eerder')
        self._laad()

    # ------------------------------------------------------------ features
    def _vul(self, X: np.ndarray) -> np.ndarray:
        """Dezelfde lege kolommen als bij het leren met 0 vullen."""
        leeg = getattr(self, "_lege_kolommen", None)
        if leeg is not None and leeg.any() and X.shape[1] == leeg.shape[0]:
            X = X.copy()
            X[:, leeg] = 0.0
        return X

    def _features(self, tijden: list[datetime], weer, niveau: float) -> np.ndarray:
        X = np.empty((len(tijden), 8))
        for r, t in enumerate(tijden):
            q = _kwartier(t)
            lokaal = t.astimezone()
            hoek = q / 96 * 2 * math.pi
            X[r] = [math.sin(hoek), math.cos(hoek), q, lokaal.weekday(), 1.0 if lokaal.weekday() >= 5 else 0.0,
                    weer.op(t), self._reeks.get(t - timedelta(days=7), np.nan), niveau]
        return X

    def _niveau(self, tot: datetime) -> float:
        """Gemiddeld verbruik per kwartier over de 7 dagen vóór 'tot'."""
        waarden = [v for t, v in self._reeks.items() if tot - timedelta(days=7) <= t < tot]
        return float(np.mean(waarden)) if waarden else np.nan

    # ------------------------------------------------------------ trainen
    def train(self, rijen: list[dict], weer) -> dict:
        from sklearn.ensemble import HistGradientBoostingRegressor
        self._reeks = {}
        for r in rijen:
            if r.get("n", 0) >= 3 and r.get(self.doel) is not None:
                self._reeks[datetime.fromisoformat(r["start"]).astimezone(timezone.utc)] = float(r[self.doel])
        tijden = sorted(self._reeks)
        dagen = len({t.astimezone().date() for t in tijden})
        if dagen < MIN_DAGEN:
            self.actief = False
            self.rapport = {"status": f"verzamelt data ({dagen} van {MIN_DAGEN} dagen)", "dagen": dagen}
            return self.rapport

        # niveau per dag één keer berekenen (sneller dan per kwartier)
        niveau_dag: dict = {}
        for t in tijden:
            d = t.astimezone().date()
            if d not in niveau_dag:
                start = datetime(d.year, d.month, d.day).astimezone()
                niveau_dag[d] = self._niveau(start)
        X = np.vstack([self._features([t], weer, niveau_dag[t.astimezone().date()]) for t in tijden])
        y = np.array([self._reeks[t] for t in tijden])

        grens = tijden[-1] - timedelta(days=TEST_DAGEN)
        tr = np.array([t <= grens for t in tijden])
        te = ~tr
        maak = lambda: HistGradientBoostingRegressor(max_iter=250, learning_rate=0.06, max_leaf_nodes=24,
                                                     min_samples_leaf=20, l2_regularization=0.5, random_state=1)
        # Kolommen zonder één bekende waarde (bijv. 'zelfde moment vorige week' in de eerste week) kan het model niet
        # gebruiken; die vullen we met 0 zodat het leren niet vastloopt.
        leeg = np.all(np.isnan(X[tr]), axis=0)
        if leeg.any():
            X = X.copy()
            X[:, leeg] = 0.0
        self._lege_kolommen = leeg
        m = maak().fit(X[tr], y[tr])
        mae_ai = float(np.mean(np.abs(m.predict(X[te]) - y[te])))

        # vergelijking: het simpele gemiddelde per (weekend, kwartier)
        som, n = defaultdict(float), defaultdict(int)
        for t, v, ok in zip(tijden, y, tr):
            if ok:
                k = (t.astimezone().weekday() >= 5, _kwartier(t))
                som[k] += v
                n[k] += 1
        basis = np.array([som[(t.astimezone().weekday() >= 5, _kwartier(t))] / max(n[(t.astimezone().weekday() >= 5, _kwartier(t))], 1)
                          for t in np.array(tijden)[te]])
        mae_basis = float(np.mean(np.abs(basis - y[te])))

        self.model = maak().fit(X, y)       # definitief model op alle data
        self.actief = mae_ai < mae_basis
        winst = (1 - mae_ai / mae_basis) * 100 if mae_basis > 0 else 0.0
        self.rapport = {"status": "actief" if self.actief else "gemiddelde is nog beter",
                        "dagen": dagen, "fout_ai": round(mae_ai, 4), "fout_gemiddelde": round(mae_basis, 4),
                        "beter_pct": round(winst, 1), **self._inzicht(weer, tijden[-1])}
        self._bewaar()
        log.info("AI %s: %s (fout %.3f vs %.3f kWh/kwartier, %+.0f%%)", self.naam, self.rapport["status"], mae_ai, mae_basis, winst)
        return self.rapport

    def _inzicht(self, weer, laatst: datetime) -> dict:
        """Wat het model over je huis geleerd heeft, in gewone getallen."""
        dagen = [laatst + timedelta(days=d) for d in range(1, 8)]
        tijden = [datetime(d.year, d.month, d.day).astimezone() + k * KW for d in (x.astimezone() for x in dagen) for k in range(96)]
        niveau = self._niveau(laatst + KW)

        class Vast:
            def __init__(s, temp): s.temp = temp
            def op(s, t): return s.temp

        koud = self.model.predict(self._vul(self._features(tijden, Vast(0.0), niveau))).sum() / 7
        warm = self.model.predict(self._vul(self._features(tijden, Vast(15.0), niveau))).sum() / 7
        normaal = self.model.predict(self._vul(self._features(tijden, weer, niveau)))
        per_uur = normaal.reshape(7, 24, 4).sum(axis=2).mean(axis=0)
        pieken = sorted(range(24), key=lambda h: -per_uur[h])[:3]
        return {"kwh_per_dag": round(float(normaal.sum() / 7), 1),
                "kwh_per_graad": round(float((koud - warm) / 15), 2),
                "piekuren": sorted(pieken), "profiel_uur": [round(float(v), 3) for v in per_uur]}

    # ------------------------------------------------------------ voorspellen
    def voorspel(self, tijden: list[datetime], weer) -> np.ndarray | None:
        if not (self.actief and self.model is not None and tijden):
            return None
        niveau = self._niveau(min(tijden))
        return np.clip(self.model.predict(self._vul(self._features(tijden, weer, niveau))), 0, None)

    def bijwerken(self, t: datetime, kwh: float):
        """Nieuwe meting toevoegen, zodat 'een week eerder' en 'niveau' actueel blijven."""
        self._reeks[t.astimezone(timezone.utc)] = kwh
        oud = t - timedelta(days=60)
        for k in [k for k in self._reeks if k < oud]:
            del self._reeks[k]

    # ------------------------------------------------------------ opslaan
    def _bewaar(self):
        try:
            self.pad.write_bytes(pickle.dumps({"model": self.model, "actief": self.actief, "rapport": self.rapport}))
        except Exception as e:
            log.warning("AI-model opslaan mislukt: %s", e)

    def _laad(self):
        try:
            d = pickle.loads(self.pad.read_bytes())
            self.model, self.actief, self.rapport = d["model"], d["actief"], d["rapport"]
        except Exception:
            pass

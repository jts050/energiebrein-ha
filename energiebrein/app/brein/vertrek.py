"""Leert wanneer je met de auto vertrekt, zodat je daar niets voor hoeft in te stellen.

Elke keer dat de stekker eruit gaat na minstens anderhalf uur aan de laadpaal, onthoudt het brein de weekdag en het tijdstip.
Na een paar keer weet het wanneer je meestal weg bent. Het plan zorgt dan dat de auto voor dat moment vol genoeg is.
Het brein neemt een vroege, veilige tijd (een kwart van de keren was je al weg), zodat de auto zelden te laat klaar is."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("vertrek")
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MIN_AANGESLOTEN = timedelta(minutes=90)      # korter is een testje of een kabel die even los zat, geen vertrek
MAX_BEWAARD = 60
MIN_PER_DAG = 3                              # zoveel keer op dezelfde weekdag, dan staat die dag vast
MIN_WERKWEEK = 8                             # of zoveel keer op werkdagen samen, dan geldt dat voor elke werkdag waar je al eens vertrok


def _percentiel(waarden: list[int], p: float) -> float:
    s = sorted(waarden)
    i = (len(s) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _tijd(minuten: float) -> str:
    m = int(minuten // 5 * 5)                # naar beneden afronden op 5 minuten: liever iets te vroeg klaar dan te laat
    return f"{m // 60:02d}:{m % 60:02d}"


class VertrekLeren:
    def __init__(self, pad: Path):
        self.pad = pad
        self.vertrekken: list[dict] = []     # {"t": iso, "dag": 0-6, "min": minuut van de dag}
        self._sinds: datetime | None = None  # sinds wanneer staat de auto aan de laadpaal
        self._vorige: bool | None = None
        try:
            z = json.loads(pad.read_text(encoding="utf-8"))
            self.vertrekken = [v for v in z.get("vertrekken", []) if isinstance(v, dict) and "dag" in v and "min" in v]
            if z.get("sinds"):
                self._sinds = datetime.fromisoformat(z["sinds"])
                self._vorige = True
        except Exception:
            pass

    def _bewaar(self) -> None:
        try:
            self.pad.write_text(json.dumps({"vertrekken": self.vertrekken[-MAX_BEWAARD:],
                                            "sinds": self._sinds.isoformat() if self._sinds else None}), encoding="utf-8")
        except Exception as e:
            log.warning("Vertrektijden bewaren mislukt: %s", e)

    def zie(self, nu: datetime, verbonden: bool) -> bool:
        """Elke meting aanroepen. Geeft True als er zojuist een vertrek is vastgelegd."""
        nu = nu.astimezone()
        vastgelegd = False
        if verbonden and self._vorige is not True:
            self._sinds = nu
            self._bewaar()
        elif not verbonden and self._vorige is True:
            if self._sinds and nu - self._sinds >= MIN_AANGESLOTEN:
                self.vertrekken.append({"t": nu.isoformat(timespec="minutes"), "dag": nu.weekday(), "min": nu.hour * 60 + nu.minute})
                self.vertrekken = self.vertrekken[-MAX_BEWAARD:]
                vastgelegd = True
            self._sinds = None
            self._bewaar()
        self._vorige = verbonden
        return vastgelegd

    def week(self) -> dict[str, str]:
        """Per weekdag de geleerde vertrektijd. Dagen waar nog te weinig over bekend is, ontbreken."""
        uit: dict[str, str] = {}
        per_dag: dict[int, list[int]] = {d: [] for d in range(7)}
        for v in self.vertrekken:
            per_dag[int(v["dag"])].append(int(v["min"]))
        werk = [m for d in range(5) for m in per_dag[d]]
        for d in range(7):
            if len(per_dag[d]) >= MIN_PER_DAG:
                uit[DAGEN[d]] = _tijd(_percentiel(per_dag[d], 0.25))
            elif d < 5 and per_dag[d] and len(werk) >= MIN_WERKWEEK:
                uit[DAGEN[d]] = _tijd(_percentiel(werk, 0.25))
        return uit

    def status(self) -> dict:
        w = self.week()
        return {"aantal": len(self.vertrekken), "week": w, "weet_iets": bool(w)}

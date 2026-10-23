"""CT-klemmen controleren met een korte proef.

Stap 1: de accu staat even stil (beginstand), daarna levert hij een halve minuut ongeveer 2,4 kW. Dat vermogen is over de
drie fasen gelijk verdeeld. Bij elke fase moet de omvormer dezelfde verandering zien als de slimme meter. Ziet hij het
omgekeerde, dan zit die klem andersom. Ziet hij maar ongeveer de helft, dan horen klem en fase niet bij elkaar: de omvormer
vermenigvuldigt de stroom dan met de spanning van een andere fase.

Stap 2: de accu staat weer stil en je zet de waterkoker aan. Die zit op één fase. De slimme meter ziet op welke fase, de
omvormer moet hem op één van zijn ingangen ongeveer even groot zien. Een ander fasenummer is niet erg (de omvormer kan
anders zijn aangesloten dan de meter nummert); half zo groot of met het omgekeerde teken wel.
"""
from __future__ import annotations

import time

ACCU_KW = 2.4


def _gem(rijen, i):
    return [sum(r[i][f] for r in rijen) / len(rijen) for f in range(3)]


def _k(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _soort(r: float) -> str:
    """Wat een verhouding omvormer/slimme meter zegt over een klem."""
    if 0.75 <= r <= 1.35:
        return "goed"
    if -1.35 <= r <= -0.75:
        return "andersom"
    if 0.2 <= abs(r) < 0.75:
        return "fase"
    if abs(r) < 0.2:
        return "niets"
    return "onduidelijk"


UITLEG = {
    "goed": "klopt",
    "andersom": "zit andersom (draai de klem om)",
    "fase": "zit op de verkeerde fase: de omvormer ziet maar ongeveer de helft",
    "niets": "meet bijna niets: zit de klem om de kabel en goed dicht?",
    "onduidelijk": "geeft een vreemde waarde",
}


class KlemTest:
    def __init__(self, brein):
        self.b = brein
        self._leeg()

    def _leeg(self):
        self.stap = None            # None, rust, accu, rust2, koker, klaar
        self.t_stap = 0.0
        self.rijen: list = []
        self.basis = None
        self.basis_accu = 0.0
        self.basis2 = None
        self.regels: list[str] = []
        self.goed = None
        self.tekst = ""

    @property
    def bezig(self) -> bool:
        return self.stap not in (None, "klaar")

    def dict(self) -> dict:
        return {"bezig": self.bezig, "klaar": self.stap == "klaar", "stap": self.stap, "tekst": self.tekst,
                "regels": self.regels, "goed": self.goed}

    def _fasen(self):
        p1 = getattr(self.b.huis.p1, "fasen", None)
        ct = getattr(self.b.huis.omvormer, "meter_fasen", None)
        return p1, ct

    def start(self) -> dict:
        p1, ct = self._fasen()
        if not self.b.huis.p1 or not self.b.huis.omvormer:
            raise ValueError("Voor de proef zijn een slimme meter (P1) en een omvormer nodig.")
        if p1 is None or ct is None:
            raise ValueError("De slimme meter of de omvormer geeft (nog) geen waarden per fase. Wacht een halve minuut en probeer opnieuw.")
        if not self.b.huis.kan_accu_sturen:
            raise ValueError("Energiebrein kan de accu van dit merk nog niet aansturen, dus de proef kan niet.")
        self._leeg()
        self._naar("rust", "Stap 1 van 2: de accu staat even stil, de beginstand wordt gemeten. Laat grote apparaten zo veel mogelijk met rust.")
        return self.dict()

    def stop(self) -> dict:
        self._leeg()
        self.tekst = "Proef gestopt."
        return self.dict()

    def _naar(self, stap: str, tekst: str):
        self.stap, self.t_stap, self.rijen, self.tekst = stap, time.monotonic(), [], tekst

    async def ronde(self, m) -> str:
        """Eén regelronde tijdens de proef. Geeft de tekst voor op Thuis."""
        p1, ct = self._fasen()
        nu = time.monotonic()
        dt = nu - self.t_stap
        a = self.b.cfg["accu"]
        if self.stap in ("rust", "rust2", "koker"):
            await self.b.huis.accu("vasthouden", 0, m.accu_soc)
        if self.stap == "accu":
            if m.accu_soc > self.b.ondergrens() + 5:
                await self.b.huis.accu("ontladen", ACCU_KW)
            else:
                await self.b.huis.accu("laden", ACCU_KW, a.get("max_soc", 100))
        meet = p1 is not None and ct is not None
        if self.stap == "rust":
            if dt > 8 and meet:
                self.rijen.append((list(p1), list(ct), m.accu_kw))
            if len(self.rijen) >= 4:
                self.basis, self.basis_accu = (_gem(self.rijen, 0), _gem(self.rijen, 1)), sum(r[2] for r in self.rijen) / len(self.rijen)
                self._naar("accu", f"Stap 1 van 2: de accu levert nu even ongeveer {_k(ACCU_KW)} kW. Dat is over de drie fasen verdeeld.")
        elif self.stap == "accu":
            if dt > 15 and meet:
                self.rijen.append((list(p1), list(ct), m.accu_kw))
            if len(self.rijen) >= 4:
                self._uitslag_accu(_gem(self.rijen, 0), _gem(self.rijen, 1), sum(r[2] for r in self.rijen) / len(self.rijen))
                self._naar("rust2", "Even wachten tot de accu weer stil staat…")
            elif dt > 90:
                self.regels.append("De accu reageerde niet op de opdracht om te leveren; stap 1 is overgeslagen.")
                self._naar("rust2", "Even wachten tot de accu weer stil staat…")
        elif self.stap == "rust2":
            if dt > 10 and meet:
                self.rijen.append((list(p1), list(ct), m.accu_kw))
            if len(self.rijen) >= 3:
                self.basis2 = (_gem(self.rijen, 0), _gem(self.rijen, 1))
                self._naar("koker", "Stap 2 van 2: zet nu de waterkoker aan (of een ander apparaat van ongeveer 2 kW op één stopcontact).")
        elif self.stap == "koker":
            if meet and self.basis2:
                d = [p1[f] - self.basis2[0][f] for f in range(3)]
                if max(d) > 1.0:
                    self.rijen.append((list(p1), list(ct), m.accu_kw))
                    self.tekst = "Waterkoker gezien, even doormeten…"
            if len(self.rijen) >= 4:
                self._uitslag_koker(_gem(self.rijen, 0), _gem(self.rijen, 1))
                self._klaar()
            elif dt > 240:
                self.regels.append("Geen waterkoker gezien binnen 4 minuten; stap 2 is overgeslagen.")
                self._klaar()
        return "CT-klemmen controleren: " + self.tekst

    def _uitslag_accu(self, p1, ct, accu_kw):
        dp = [p1[f] - self.basis[0][f] for f in range(3)]
        dc = [ct[f] - self.basis[1][f] for f in range(3)]
        da = accu_kw - self.basis_accu
        if abs(da) < 1.0:
            self.regels.append(f"Stap 1: de accu veranderde maar {_k(abs(da))} kW; te weinig om de klemmen te beoordelen.")
            return
        if abs(sum(dp) - da) > 0.7:
            self.regels.append("Stap 1: tijdens de meting veranderde ook het verbruik in huis (een apparaat ging aan of uit). "
                               "De uitslag van deze stap is daardoor onzeker; doe de proef liever opnieuw met minder apparaten aan.")
        oordeel = []
        for f in range(3):
            if abs(dp[f]) < 0.3:
                continue
            r = dc[f] / dp[f]
            soort = _soort(r)
            oordeel.append(soort)
            self.regels.append(f"Stap 1, L{f + 1}: slimme meter {_k(dp[f])} kW, omvormer {_k(dc[f])} kW: {UITLEG[soort]}.")
        self._stap1 = oordeel

    def _uitslag_koker(self, p1, ct):
        dp = [p1[f] - self.basis2[0][f] for f in range(3)]
        dc = [ct[f] - self.basis2[1][f] for f in range(3)]
        fase = max(range(3), key=lambda f: dp[f])
        kanaal = max(range(3), key=lambda f: abs(dc[f]))
        P = dp[fase]
        r = dc[kanaal] / P if P else 0.0
        soort = _soort(r)
        self._stap2 = soort
        if soort == "goed":
            extra = "" if kanaal == fase else (f" (de omvormer noemt die fase L{kanaal + 1}; dat is niet erg, de omvormer is dan "
                                               "anders aangesloten dan de meter nummert)")
            self.regels.append(f"Stap 2: de waterkoker ({_k(P)} kW) zit op L{fase + 1}. De omvormer ziet hem even groot{extra}: klopt.")
        elif soort == "andersom":
            self.regels.append(f"Stap 2: de waterkoker ({_k(P)} kW) zit op L{fase + 1}. De omvormer ziet hem op ingang L{kanaal + 1} "
                               f"met het omgekeerde teken: die klem zit andersom.")
        elif soort == "fase":
            self.regels.append(f"Stap 2: de waterkoker ({_k(P)} kW) zit op L{fase + 1}, maar de omvormer ziet op ingang L{kanaal + 1} "
                               f"maar {_k(dc[kanaal])} kW. De klem om de kabel van L{fase + 1} hoort bij een andere fase-ingang "
                               "van de omvormer. Wissel de klemmen tot de omvormer de waterkoker even groot ziet.")
        else:
            self.regels.append(f"Stap 2: de waterkoker ({_k(P)} kW) zit op L{fase + 1}, maar de omvormer ziet hem nergens goed. "
                               f"Zit de klem van die fase om de kabel en goed dicht?")

    def _klaar(self):
        s1 = getattr(self, "_stap1", None) or []
        s2 = getattr(self, "_stap2", None)
        uitslagen = s1 + ([s2] if s2 else [])
        self.goed = bool(uitslagen) and all(x == "goed" for x in uitslagen)
        self.stap = "klaar"
        if self.goed:
            self.tekst = ("Alle klemmen kloppen. De omvormer kan de accu zelf regelen: je kunt de P1-regeling uitzetten.")
        elif not uitslagen:
            self.tekst = "De proef gaf geen duidelijke uitslag. Probeer het opnieuw met zo min mogelijk apparaten aan."
        else:
            self.tekst = ("Niet alle klemmen kloppen. Laat de P1-regeling aan tot dit is opgelost; doe de proef daarna nog een keer.")
        self.b.logboek.schrijf("handmatig", "CT-klemmen gecontroleerd: " + ("alles klopt" if self.goed else "niet alles klopt"), "omvormer")

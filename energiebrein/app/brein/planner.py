"""De planner: verdeelt per kwartier de stroom over accu, auto en warmtepomp, zodat je zo weinig mogelijk betaalt.

Volgorde:
1. Warmtepomp: extra stoken op goedkope kwartieren, wachten op dure (binnen de grenzen die jij instelt).
2. Auto: de benodigde kWh vóór vertrek in de goedkoopste kwartieren, zonne-overschot eerst.
3. Accu: dynamisch programmeren over de laadtoestand. Dit vindt de goedkoopste laad- en ontlaadvolgorde
   voor de hele horizon (tot het einde van de bekende prijzen, meestal morgen 24:00).
"""
from __future__ import annotations
import math

import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone

from .model import Kwartier, Bediening

Q = 0.25  # uur per kwartier


@dataclass
class AutoStatus:
    verbonden: bool
    geladen_kwh: float = 0.0


def _volgende(tijd_str: str, na: datetime) -> datetime:
    h, m = (int(x) for x in tijd_str.split(":"))
    lokaal = na.astimezone()
    for i in range(2):
        d = lokaal.date() + timedelta(days=i)
        t = datetime(d.year, d.month, d.day, h, m).astimezone()     # per dag de juiste zomer-/wintertijd
        if t > lokaal:
            return t.astimezone(timezone.utc)
    return t.astimezone(timezone.utc)


def _euro(x: float) -> str:
    return f"€ {x:.2f}".replace(".", ",")


def zon_in_accu(c: dict) -> bool:
    """Gaat zonne-overschot altijd eerst de accu in (zolang die ruimte heeft)? Standaard wel."""
    return c.get("zon_altijd_in_accu", True) not in (False, "uit", "nee", 0)


def mag_verkopen(c: dict) -> bool:
    """Mag de accu stroom aan het net verkopen? Standaard wel; uit = de accu voedt alleen het eigen huis."""
    return c.get("terugverkopen", True) not in (False, "uit", "nee", 0)


class Planner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ondergrens: float | None = None   # ondergrens die de omvormer zelf hanteert (%), als die hoger is
        self.storm: dict = {}                  # kwartier-start → reden (onweer, windstoten): dan de accu vol houden
        self.eind_soc: float | None = None     # alleen voor de terugblik: eindig met deze lading (%), dan is de vergelijking eerlijk
        self._e_start = 0.0
        self._eindwaarde = 0.0

    def ondergrens_pct(self) -> float:
        """De accu ontlaadt niet verder dan de hoogste van twee grenzen: die in Energiebrein en die in de omvormer."""
        return max(float(self.cfg["accu"]["min_soc"]), float(self.ondergrens or 0))

    # ---------------------------------------------------------------
    def maak(self, kw: list[Kwartier], soc_nu: float, bed: Bediening, auto: AutoStatus,
             wp_dag_kwh) -> dict:
        if not kw:
            return {"kwartieren": [], "samenvatting": {}}
        self._warmtepomp(kw, bed, wp_dag_kwh)
        self._auto(kw, bed, auto)
        self._accu(kw, soc_nu)
        return {"kwartieren": kw, "samenvatting": self._samenvatting(kw, soc_nu)}

    # ---------------------------------------------------------------
    @staticmethod
    def _marginaal(k: Kwartier, extra: float = 0.0) -> float:
        """Wat een extra kWh in dit kwartier ongeveer kost: teruglevertarief bij overschot, anders inkoop."""
        overschot = k.zon_kwh - k.huis_kwh - k.wp_kwh - k.auto_kwh - extra
        return k.teruglever if overschot > 0 else k.inkoop

    def _warmtepomp(self, kw: list[Kwartier], bed: Bediening, wp_dag_kwh) -> None:
        c = self.cfg.get("warmtepomp", {})
        if c.get("type", "uit") == "uit":
            return
        alleen_meten = c.get("type") == "meter"            # alleen meten: verbruik verwachten, niets sturen
        p = float(c.get("vermogen_kw") or 3.0) * Q
        dagen: dict = {}
        for k in kw:
            dagen.setdefault(k.start.astimezone().date(), []).append(k)
        for dag, ks in dagen.items():
            totaal = wp_dag_kwh(ks[0].start) * len(ks) / 96   # deel van de dag binnen de horizon
            if totaal <= 0:
                continue
            nb, nw = int(c.get("boost_uren_per_dag", 0) * 4 * len(ks) / 96), int(c.get("blokkeer_uren_per_dag", 0) * 4 * len(ks) / 96)
            stand = {id(k): "normaal" for k in ks}
            if alleen_meten:
                pass
            elif bed.warmtepomp == "auto":
                volgorde = sorted(ks, key=self._marginaal)
                for k in volgorde[:nb]:
                    stand[id(k)] = "boost"
                self._kies_wachten(ks, stand, nw, c)
            elif bed.warmtepomp in ("boost", "wacht"):
                for k in ks[:4]:
                    stand[id(k)] = bed.warmtepomp
            # Boost = zo'n 2,5x het gemiddelde (warmer stoken of boiler opwarmen), wacht = even niets.
            # Het dagtotaal blijft gelijk: wat eerder gestookt wordt, hoeft later niet meer.
            gem = totaal / len(ks)
            boost = min(p, gem * 2.5)
            vast = boost * sum(1 for k in ks if stand[id(k)] == "boost")
            normaal = [k for k in ks if stand[id(k)] == "normaal"]
            rest = max(totaal - vast, 0) / max(len(normaal), 1)
            for k in ks:
                s = stand[id(k)]
                k.wp_stand = s
                k.wp_kwh = boost if s == "boost" else (0.0 if s == "wacht" else min(rest, p))

    def _kies_wachten(self, ks: list[Kwartier], stand: dict, aantal: int, c: dict) -> None:
        """De duurste kwartieren 'wachten', maar met grenzen voor comfort:
        - nooit langer dan max_wacht_achter_elkaar_uur achter elkaar, en daarna minstens 2 uur normaal;
        - niet in de comforttijden (douchen) of het uur ervoor, zodat de boiler dan warm is;
        - niet als het buiten kouder is dan niet_wachten_onder_c."""
        max_run = max(1, int(float(c.get("max_wacht_achter_elkaar_uur", 1.5)) * 4))
        rust = 8                                                     # 2 uur normaal tussen twee wachtblokken
        grens_c = c.get("niet_wachten_onder_c", 2)
        comfort = []
        for blok in c.get("comfort_tijden") or []:
            try:
                van, tot = blok.split("-")
                h1, m1 = (int(x) for x in van.split(":"))
                h2, m2 = (int(x) for x in tot.split(":"))
                comfort.append(((h1 * 60 + m1 - 60) % 1440, h2 * 60 + m2))   # het uur ervoor hoort erbij
            except Exception:
                continue
        def in_comfort(k):
            lokaal = k.start.astimezone()
            mnu = lokaal.hour * 60 + lokaal.minute
            return any((a <= mnu < b) if a <= b else (mnu >= a or mnu < b) for a, b in comfort)
        temp = getattr(self, "temp", None)
        pos = {id(k): i for i, k in enumerate(ks)}
        gekozen = 0
        for k in sorted(ks, key=self._marginaal, reverse=True):
            if gekozen >= aantal:
                break
            if stand[id(k)] != "normaal" or in_comfort(k):
                continue
            if temp is not None and grens_c is not None:
                try:
                    if temp(k.start) < float(grens_c):
                        continue
                except Exception:
                    pass
            i = pos[id(k)]
            # blok waar dit kwartier bij zou horen
            lo = i
            while lo > 0 and stand[id(ks[lo - 1])] == "wacht":
                lo -= 1
            hi = i
            while hi + 1 < len(ks) and stand[id(ks[hi + 1])] == "wacht":
                hi += 1
            if hi - lo + 1 > max_run:
                continue
            buren = [ks[j] for j in list(range(max(0, lo - rust), lo)) + list(range(hi + 1, min(len(ks), hi + 1 + rust)))]
            if any(stand[id(x)] == "wacht" for x in buren):
                continue
            stand[id(k)] = "wacht"
            gekozen += 1

    def _auto(self, kw: list[Kwartier], bed: Bediening, auto: AutoStatus) -> None:
        c = self.cfg.get("laadpaal", {})
        if c.get("type", "uit") == "uit" or not auto.verbonden or bed.laadpaal == "stop":
            return
        fasen = c["fasen"]
        p_min, p_max = c["min_stroom_a"] * 230 * fasen / 1000, c["max_stroom_a"] * 230 * fasen / 1000
        nodig = max(bed.auto_doel_kwh - auto.geladen_kwh, 0.0)
        if nodig <= 0.05:
            return
        alleen_zon = bed.laadpaal == "zon"
        if bed.auto_deadline:
            deadline = datetime.fromisoformat(bed.auto_deadline)
        elif bed.auto_vertrek:                      # alleen een tijd: het eerstvolgende moment op die tijd
            deadline = _volgende(bed.auto_vertrek, kw[0].start)
        elif bed.laadpaal == "nu":
            deadline = kw[-1].start + timedelta(minutes=15)
        else:
            # geen vertrek gepland: alleen op zonne-overschot, of (als je dat kiest) de goedkoopste kwartieren
            deadline = kw[-1].start + timedelta(minutes=15)
            alleen_zon = alleen_zon or bed.auto_zonder == "zon"
        kandidaten = [k for k in kw if k.start < deadline]
        if bed.laadpaal == "nu":
            for k in kandidaten:
                e = min(p_max * Q, nodig)
                k.auto_kwh, nodig = e, nodig - e
                if nodig <= 0:
                    break
            return
        # stukken: eerst overschot (teruglevertarief), dan de rest (inkoop)
        stukken = []
        for k in kandidaten:
            over = max(k.zon_kwh - k.huis_kwh - k.wp_kwh, 0.0)
            if over > 0.01:
                stukken.append((k.teruglever, min(over, p_max * Q), k))
            if not alleen_zon:
                stukken.append((k.inkoop, p_max * Q - min(over, p_max * Q), k))
        for prijs, ruimte, k in sorted(stukken, key=lambda s: (s[0], s[2].start)):
            if nodig <= 0:
                break
            e = min(ruimte, nodig, p_max * Q - k.auto_kwh)
            if e > 0:
                k.auto_kwh += e
                nodig -= e
        for k in kandidaten:  # onder het minimum kan een laadpaal niet: ophogen (bij alleen zon: dan maar niet)
            if 0 < k.auto_kwh < p_min * Q:
                k.auto_kwh = 0.0 if alleen_zon and k.zon_kwh - k.huis_kwh - k.wp_kwh < p_min * Q * 0.8 else p_min * Q

    def _accu(self, kw: list[Kwartier], soc_nu: float) -> None:
        """Dynamisch programmeren over de laadtoestand in stapjes van ~50 Wh (gevectoriseerd met numpy)."""
        c = self.cfg["accu"]
        cap = c["capaciteit_kwh"]
        lo, hi = cap * self.ondergrens_pct() / 100, cap * c["max_soc"] / 100
        if hi - lo < 0.5:                                  # grenzen te dicht op elkaar: altijd iets ruimte houden
            hi = min(cap, lo + 0.5)
        aftoppen_mag = self.cfg.get("omvormer", {}).get("aftoppen_bij_negatieve_prijs", True)
        eta = math.sqrt(c["rendement"])
        # Per geladen kWh: slijtage plus de winst die het minstens moet opleveren. Zo laadt en ontlaadt de accu niet
        # voor een paar cent verschil, waar na laadverlies, slijtage en een verkeerde voorspelling niets van overblijft.
        slijtage = float(c.get("slijtage_per_kwh", 0.02))
        min_winst = max(float(c.get("min_winst_per_kwh", 0.0) or 0.0), 0.0)
        verkopen = mag_verkopen(c)
        zon_eerst = zon_in_accu(c)
        N = max(10, min(400, int((hi - lo) / 0.05)))
        stap = (hi - lo) / N
        up, dn = max(1, int(c["max_laden_kw"] * Q / stap)), max(1, int(c["max_ontladen_kw"] * Q / stap))
        T = len(kw)
        rest = sorted(k.inkoop for k in kw[-16:])
        eindwaarde = rest[len(rest) // 2] * eta * 0.85   # wat een kWh in de accu na de horizon waard is
        idx = np.arange(N + 1)
        V = -(idx * stap) * eindwaarde
        if self.eind_soc is not None:
            # terugblik: net zoveel lading aan het eind als er echt in zat; elke kWh verschil kost € 1
            doel_eind = int(round((min(max(cap * self.eind_soc / 100, lo), hi) - lo) / stap))
            V = np.abs(idx - doel_eind) * stap * 1.0
        keuze = np.zeros((T, N + 1), dtype=np.int16)
        deltas = np.arange(-dn, up + 1)
        na = [None] * T            # kosten-tot-het-einde na elk kwartier (voor de waarde van een kWh)
        for t in range(T - 1, -1, -1):
            na[t] = V
            k = kw[t]
            netto = k.huis_kwh + k.wp_kwh + k.auto_kwh - k.zon_kwh
            d = deltas * stap
            g = netto + np.where(d > 0, d / eta, d * eta)                     # netstroom per keuze
            terug = 0.0 if (aftoppen_mag and k.teruglever < 0) else k.teruglever
            # Krijg je geld voor stroom (negatieve inkoopprijs), dan geen extra winstdrempel: elke kWh erin levert al op.
            slijt = slijtage + (min_winst if k.inkoop >= 0 else 0.0)
            kost = np.where(g > 0, g * k.inkoop, g * terug) + np.where(d > 0, d * slijt, 0.0)   # per keuze, incl. slijtage
            doel = idx[:, None] + deltas[None, :]                              # (N+1, keuzes)
            geldig = (doel >= 0) & (doel <= N)
            if not verkopen:
                # niet terugverkopen: ontladen mag alleen zolang het de vraag van het huis dekt, nooit naar het net
                geldig &= ~((d < 0) & (g < min(netto, 0.0) - 1e-9))[None, :]
            if zon_eerst and netto < -1e-9 and not (aftoppen_mag and k.teruglever < 0):
                # zonne-overschot: niet terugleveren zolang de accu het kan opnemen (vol laden of de accu zit vol)
                geldig &= (g >= -1e-9)[None, :] | (deltas == up)[None, :] | (doel >= N)
            totaal = np.where(geldig, kost[None, :] + V[np.clip(doel, 0, N)], np.inf)
            if k.start in self.storm:
                # Stormstand: elke kWh onder (bijna) vol kost in dit kwartier € 1. Zo laadt het plan vooraf vol en houdt het
                # de lading vast, maar lukt dat niet helemaal (te weinig tijd), dan loopt het plan niet vast.
                tekort = np.maximum(0, int(N * 0.95) - np.clip(doel, 0, N)) * stap
                totaal = totaal + tekort * 1.0
            # bij gelijke kosten: liever niets doen (minder slijtage)
            totaal += np.abs(deltas)[None, :] * 1e-6
            beste = np.argmin(totaal, axis=1)
            keuze[t] = deltas[beste]
            V = totaal[idx, beste]
        i = int(round((min(max(cap * soc_nu / 100, lo), hi) - lo) / stap))
        self._e_start, self._eindwaarde = lo + i * stap, eindwaarde
        for t, k in enumerate(kw):
            dj = int(keuze[t][i])
            d = dj * stap
            k.accu_kwh = d
            i += dj
            k.soc_na = (lo + i * stap) / cap * 100
            # Waarde van één extra kWh in de accu na dit kwartier = hoeveel goedkoper de rest van het plan wordt.
            Vn = na[t]
            if i < N:
                k.waarde_kwh = float((Vn[i] - Vn[i + 1]) / stap)
            else:
                k.waarde_kwh = float((Vn[i - 1] - Vn[i]) / stap)
            g = k.huis_kwh + k.wp_kwh + k.auto_kwh - k.zon_kwh + (d / eta if d > 0 else d * eta)
            k.aftoppen = aftoppen_mag and k.teruglever < 0 and g < 0
            k.net_kwh = 0.0 if k.aftoppen else g
        self._redenen(kw)

    def _redenen(self, kw: list[Kwartier]) -> None:
        eta = math.sqrt(self.cfg["accu"]["rendement"])
        duur = sorted(k.inkoop for k in kw)
        hoog = duur[int(len(duur) * 0.75)] if duur else 0
        for idx, k in enumerate(kw):
            over = k.zon_kwh - k.huis_kwh - k.wp_kwh - k.auto_kwh
            later = max((x.inkoop for x in kw[idx:idx + 48]), default=k.inkoop)
            if k.start in self.storm and k.accu_actie != "ontladen":
                k.reden = f"{self.storm[k.start].capitalize()} verwacht: de accu blijft vol voor als de stroom uitvalt."
            elif k.accu_actie == "laden" and k.inkoop < 0:
                k.reden = f"Je krijgt nu geld voor stroom ({_euro(-k.inkoop)} per kWh): de accu laadt uit het net en je wordt ervoor betaald."
            elif k.aftoppen:
                k.reden = f"Terugleveren kost nu geld ({_euro(k.teruglever)}), panelen worden afgetopt."
            elif k.accu_actie == "laden" and over >= 0.8 * k.accu_kwh / eta:
                k.reden = "Zonne-overschot gaat de accu in in plaats van het net op."
            elif k.accu_actie == "laden" and over > 0.05:
                k.reden = (f"Laadt met zon en goedkope stroom uit het net ({_euro(k.inkoop)}); "
                           f"later loopt de prijs op tot {_euro(later)}.")
            elif k.accu_actie == "laden":
                k.reden = f"Goedkoop laden voor {_euro(k.inkoop)}; later loopt de prijs op tot {_euro(later)}."
            elif k.accu_actie == "ontladen" and k.net_kwh < -0.02:
                duurste = k.inkoop >= max((x.inkoop for x in kw[idx:idx + 96]), default=k.inkoop) - 0.005
                k.reden = (f"Accu verkoopt aan het net tegen {_euro(k.teruglever)}"
                           + (", het duurste moment van het komende etmaal." if duurste else "; dat levert nu meer op dan de lading bewaren."))
            elif k.accu_actie == "ontladen":
                k.reden = f"Accu voedt het huis, inkoop zou {_euro(k.inkoop)} kosten."
            elif k.inkoop >= hoog and k.soc_na <= self.ondergrens_pct() + 1:
                k.reden = f"Dure periode ({_euro(k.inkoop)}), maar de accu is leeg (op de ondergrens): het huis haalt stroom uit het net."
            elif k.inkoop >= hoog:
                k.reden = "Dure periode, maar de accu bewaart zijn lading voor een nog duurder moment."
            else:
                k.reden = "Prijsverschil te klein om te laden of te ontladen."
            if k.auto_kwh > 0:
                k.reden += " Auto laadt" + (" op zon." if over > 0 else f" voor {_euro(k.inkoop)}.")
            if k.wp_stand == "boost":
                k.reden += " Warmtepomp stookt extra (goedkoop)."
            elif k.wp_stand == "wacht":
                k.reden += " Warmtepomp wacht even (duur)."

    def _samenvatting(self, kw: list[Kwartier], soc_nu: float) -> dict:
        met = sum(k.net_kwh * (k.inkoop if k.net_kwh > 0 else k.teruglever) for k in kw)
        zonder = 0.0
        for k in kw:
            g = k.huis_kwh + k.wp_kwh + k.auto_kwh - k.zon_kwh
            zonder += g * (k.inkoop if g > 0 else k.teruglever)
        # Wat er aan het eind meer (of minder) in de accu zit dan nu, telt mee: die lading is betaald maar nog niet gebruikt.
        voorraad = kw[-1].soc_na / 100 * self.cfg["accu"]["capaciteit_kwh"] - self._e_start
        return {"kosten_plan": round(met, 2), "kosten_zonder_accu": round(zonder, 2),
                "besparing": round(zonder - met + voorraad * self._eindwaarde, 2), "voorraad_kwh": round(voorraad, 1),
                "kwartieren": len(kw),
                "tot": (kw[-1].start + timedelta(minutes=15)).isoformat(),
                "goedkoopste": min(kw, key=lambda k: k.inkoop).start.isoformat(),
                "duurste": max(kw, key=lambda k: k.inkoop).start.isoformat()}

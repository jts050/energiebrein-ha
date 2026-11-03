"""Kleine, aaneengesloten warmteblokken. Geen wijziging van bedrijfsmodi."""
from datetime import datetime, timedelta
import math


def goedkoop_blok(kwartieren, aantal=4):
    """Kies een heel blok; ontbrekende kwartieren/tarieven worden niet ingevuld."""
    opties = []
    for i in range(len(kwartieren) - aantal + 1):
        blok = kwartieren[i:i + aantal]
        if any(b.start - a.start != timedelta(minutes=15) for a, b in zip(blok, blok[1:])):
            continue
        if not all(math.isfinite(k.inkoop) and math.isfinite(k.teruglever) for k in blok):
            continue
        kosten = sum(k.teruglever if k.zon_kwh > k.huis_kwh + k.wp_kwh + k.auto_kwh else k.inkoop for k in blok)
        opties.append((kosten, i, blok))
    return min(opties, key=lambda x: (x[0], x[1]))[2] if opties else []


def plan_warmwater(kw, cfg, keuzes):
    """Een uur bescheiden bijverwarmen in de vier uur vóór een comfortperiode.

    Een gemaakte keuze blijft tijdens deze planhorizon vaststaan. De normale
    warmwaterregeling en het legionellaprogramma blijven altijd actief.
    """
    if not kw or not cfg.get('warmwater_slim') or cfg.get('type') != 'ha_thermostaat':
        return
    begin, einde = kw[0].start, kw[-1].start + timedelta(minutes=15)
    for key in list(keuzes):
        if keuzes[key][-1] + timedelta(minutes=15) <= begin:
            # Bewaar afgeronde keuzes tot de deadline: geen tweede blok plannen.
            if key <= begin:
                del keuzes[key]
    dagen = {k.start.astimezone().date() for k in kw}
    deadlines = set()
    for dag in dagen:
        for tekst in cfg.get('comfort_tijden') or []:
            try:
                h, m = map(int, tekst.split('-')[0].split(':'))
                deadline = datetime(dag.year, dag.month, dag.day, h, m).astimezone()
                if begin < deadline <= einde:
                    deadlines.add(deadline)
            except (ValueError, TypeError):
                continue
    energie = max(0.0, min(float(cfg.get('warmwater_extra_kwh', 1.0)), 5.0))
    for deadline in sorted(deadlines):
        if deadline not in keuzes:
            al_gekozen = {t for tijden in keuzes.values() for t in tijden}
            kandidaten = [k for k in kw if deadline - timedelta(hours=4) <= k.start
                           and k.start + timedelta(minutes=15) <= deadline and k.start not in al_gekozen]
            blok = goedkoop_blok(kandidaten)
            if not blok:
                continue
            keuzes[deadline] = [k.start for k in blok]
        gekozen = set(keuzes[deadline])
        for k in kw:
            if k.start in gekozen:
                k.water_boost = True
                k.water_kwh = energie / 4
                k.wp_kwh += k.water_kwh
                k.water_tot = (max(gekozen) + timedelta(minutes=15)).isoformat()
                k.water_reden = ('Warm water voorbereiden voor ' + deadline.strftime('%H:%M')
                    + '; gunstig aaneengesloten uur op basis van zon en stroomtarieven. '
                    + 'Maximaal 2 °C boven je eigen instelling; verbruik is een schatting.')


def warmteblokken(kw, cfg):
    """Uitleg wordt uit dezelfde kwartieren opgebouwd als de uitvoering."""
    uit = []
    for soort in ('huis', 'water'):
        huidig = None
        for k in kw:
            actief = k.water_boost if soort == 'water' else k.wp_stand in ('boost', 'wacht')
            stand = 'boost' if soort == 'water' else k.wp_stand
            if not actief:
                huidig = None
                continue
            if huidig is None or huidig['tot'] != k.start.isoformat() or huidig['stand'] != stand:
                delta = (2.0 if soort == 'water' else min(float(cfg.get('verhoging', 1)), .5)
                         if cfg.get('rustig_voorverwarmen') else float(cfg.get('verhoging', 1)))
                huidig = {'soort': soort, 'stand': stand, 'start': k.start.isoformat(),
                    'tot': '', 'kwh': 0, 'delta': delta if stand == 'boost' else -float(cfg.get('verlaging', 1)),
                    'reden': k.water_reden if soort == 'water' else
                        ('Een gunstig stroommoment; je eigen kamertemperatuur blijft het uitgangspunt.'
                         if stand == 'boost' else 'Duur stroommoment, binnen de ingestelde comfortgrenzen.')}
                uit.append(huidig)
            huidig['tot'] = (k.start + timedelta(minutes=15)).isoformat()
            huidig['kwh'] += k.water_kwh if soort == 'water' else max(0, k.wp_kwh - k.water_kwh)
    return sorted(uit, key=lambda b: b['start'])


def avondprofiel(rijen):
    """Alleen verse, aantoonbaar gemeten warmtepompwaarden; geen driverschatting.

    Vergelijk complete avonden (16–24 uur), zodat een ontbrekend uur geen
    kunstmatige avondpiek oplevert. Drie avonden is een eerste indicatie.
    """
    uren = {}
    for r in rijen:
        try:
            t = datetime.fromisoformat(r['start']).astimezone()
            sec = float(r.get('wp_meetseconden') or 0)
            kwh = float(r.get('wp_gemeten_kwh') or 0)
            if not 16 <= t.hour <= 23 or not math.isfinite(kwh) or sec <= 0:
                continue
            item = uren.setdefault((t.date(), t.hour), [0, 0])
            item[0] += sec; item[1] += kwh
        except (ValueError, TypeError, KeyError):
            continue
    dagen = {d for d, _ in uren if all(uren.get((d, h), [0])[0] >= 3300 for h in range(16, 24))}
    if len(dagen) < 3:
        return {'voldoende': False, 'avonden': len(dagen), 'uren': [],
                'reden': 'Eerst minimaal drie volledig gemeten avonden (16:00–24:00). Schattingen en meetgaten tellen niet mee.'}
    profiel = [{'uur': h, 'gem_kw': sum(uren[d, h][1] / uren[d, h][0] * 3600 for d in dagen) / len(dagen)}
               for h in range(16, 24)]
    piek = max(profiel, key=lambda r: r['gem_kw'])
    return {'voldoende': True, 'avonden': len(dagen), 'uren': profiel,
            'piek_uur': piek['uur'], 'piek_kw': piek['gem_kw'],
            'reden': 'Gemiddelde van volledig gemeten avonden; een verbruikspatroon, geen bewezen besparing.'}


def prijsvoordeel(kw, dagverbruik, cfg):
    """Modelvergelijking met dezelfde warmtevraag gelijkmatig over de horizon.

    Geen gemeten factuurkorting: geen COP-, weer- of warmteverliescorrectie en
    geen winst van de accu meetellen. Warmwater-extra wordt apart vermeld.
    """
    if cfg.get('type', 'uit') == 'uit':
        return {}
    dagen = {}
    for k in kw:
        dagen.setdefault(k.start.astimezone().date(), []).append(k)
    normaal, gepland = 0.0, 0.0
    for ks in dagen.values():
        basis = min(max(0, dagverbruik(ks[0].start)) / 96, float(cfg.get('vermogen_kw') or 3) / 4)
        for k in ks:
            over = max(0, k.zon_kwh - k.huis_kwh)
            def kost(energie):
                return min(energie, over) * k.teruglever + max(0, energie - over) * k.inkoop
            normaal += kost(basis)
            gepland += kost(k.wp_kwh)
    return {'prijsvoordeel_huis': normaal - gepland, 'kosten_normaal': normaal,
            'kosten_gepland_huis': gepland,
            'uitleg': 'Schatting tegenover gelijkmatig verbruik bij dezelfde warmtevraag, zonder accu. '
                      'Extra warmteverlies en veranderend rendement zijn nog niet gemeten. '
                      'Daarom is dit geen bewezen besparing.'}

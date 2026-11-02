import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'energiebrein' / 'app'))
from brein.model import Bediening, Kwartier, Meting
from brein.planner import Planner, AutoStatus
from brein.geschiedenis import Geschiedenis
from brein.opbrengst import opbrengst, kosten_periode
from brein.config import laad

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def test_temporary_mode_survives_restart_and_expires_at_deadline():
    b = Bediening()
    b.kies('accu', 'vasthouden', 60, NOW)
    restored = Bediening(**json.loads(json.dumps(b.dict())))
    assert restored.verlopen(NOW + timedelta(minutes=59)) == []
    assert restored.accu == 'vasthouden'
    assert restored.verlopen(NOW + timedelta(minutes=60)) == ['accu']
    assert restored.accu == 'auto' and restored.tot == {}


def test_new_command_replaces_old_timer_and_slim_cancels():
    b = Bediening()
    b.kies('laadpaal', 'nu', 30, NOW)
    b.kies('laadpaal', 'zon', 120, NOW + timedelta(minutes=10))
    assert b.verlopen(NOW + timedelta(minutes=31)) == []
    assert b.laadpaal == 'zon'
    b.kies('laadpaal', 'auto', 30, NOW)
    assert not b.tot


def test_expiry_is_independent_per_device_and_handles_corrupt_date():
    b = Bediening()
    b.kies('accu', 'laden', 30, NOW)
    b.kies('warmtepomp', 'boost', 120, NOW)
    assert b.verlopen(NOW + timedelta(minutes=31)) == ['accu']
    assert b.warmtepomp == 'boost'
    b.tot['warmtepomp'] = 'not-a-date'
    assert b.verlopen(NOW) == ['warmtepomp']


def quarters():
    return [Kwartier(NOW + timedelta(minutes=15*i), .2, .8 if i < 8 else .2, .1,
                     zon_kwh=.1, huis_kwh=.25) for i in range(24)]


def planner(reserve=True):
    cfg = copy.deepcopy(laad())
    cfg['accu'].update(capaciteit_kwh=10, min_soc=10, max_soc=90,
                       onzekerheidsreserve=reserve, max_laden_kw=3, max_ontladen_kw=3,
                       terugverkopen=False)
    cfg['warmtepomp']['type'] = 'uit'
    p = Planner(cfg)
    p.onzekerheid = {'zon': .5, 'huis': .5}
    return p


def test_reserve_never_creates_energy_below_reserve():
    p = planner()
    ks = quarters()
    p.maak(ks, 12, Bediening(), AutoStatus(False), lambda _: 0)
    assert p.reserve_info['extra_pct'] == 10
    assert p._e_start <= 1.25  # 12%, not silently lifted to the 20% reserve
    assert all(k.accu_kwh >= 0 or k.soc_na >= p.reserve_pct - 1e-6 for k in ks)
    energy = p._e_start
    for k in ks:
        energy += k.accu_kwh
        assert k.soc_na == pytest.approx(energy / 10 * 100)
        assert 10 - 1e-6 <= k.soc_na <= 90 + 1e-6


def test_reserve_off_keeps_existing_floor_and_resets_previous_reserve():
    p = planner()
    p.maak(quarters(), 50, Bediening(), AutoStatus(False), lambda _: 0)
    assert p.reserve_pct > 10
    p.cfg['accu']['onzekerheidsreserve'] = False
    p.maak(quarters(), 50, Bediening(), AutoStatus(False), lambda _: 0)
    assert p.reserve_pct == 10 and not p.reserve_info['actief']


def test_reserve_respects_hardware_floor_and_leaves_room_at_top():
    p = planner()
    p.ondergrens = 85
    p.maak(quarters(), 88, Bediening(), AutoStatus(False), lambda _: 0)
    assert p.reserve_pct == 88
    assert p.reserve_info['extra_pct'] == 3


def test_reserve_uses_fallback_only_when_history_is_unknown():
    p = planner()
    p.onzekerheid = {}
    p._reserve(quarters())
    assert p.reserve_info['actief']
    p.onzekerheid = {'zon': 0, 'huis': 0}
    p._reserve(quarters())
    assert not p.reserve_info['actief']


class Prices:
    def inkoop(self, *_): return .4
    def teruglever(self, *_): return .1


def row(**kw):
    return dict(start=NOW.isoformat(), n=30, huis_kwh=1, wp_kwh=0, auto_kwh=0,
                kosten=.2, kosten_zonder=.4, soc=50, inkoop=.4, terug=.1,
                beurs=.2, accu_geladen_kwh=2, **kw)


def test_costs_separate_cash_inventory_and_wear():
    before = row()
    before.update(start=(NOW - timedelta(days=1)).isoformat(), soc=40)
    d = opbrengst([before, row()], Prices(), 30, 10, 1, .02)['dagen'][-1]
    assert d['door_accu_kas'] == .2
    assert d['lading_eur'] == .4
    assert d['slijtage_eur'] == .04
    assert d['door_accu_netto'] == .56
    assert d['slijtage_volledig'] is True


def test_old_history_does_not_present_unknown_wear_as_zero():
    r = row()
    r.pop('accu_geladen_kwh')
    d = opbrengst([r], Prices())['dagen'][0]
    assert d['door_accu_netto'] is None
    assert d['slijtage_volledig'] is False


def test_missing_recorded_tariffs_are_marked_estimated():
    r = row()
    r['inkoop'] = None
    assert opbrengst([r], Prices())['dagen'][0]['prijs_geschat'] is True


def test_charge_then_discharge_does_not_cancel_wear_measurement(tmp_path):
    g = Geschiedenis(tmp_path / 'history.db')
    g.meet(Meting(NOW, accu_kw=2, accu_soc=50), .4, .1, 60)
    g.meet(Meting(NOW + timedelta(minutes=1), accu_kw=-2, accu_soc=50), .4, .1, 60)
    r = g.rijen(NOW)[0]
    assert r['accu_kwh'] == pytest.approx(0)
    assert r['accu_geladen_kwh'] == pytest.approx(2/60)


def test_timer_request_rejects_out_of_range_duration():
    from brein.web import Keuze
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Keuze(apparaat='accu', stand='laden', minuten=0)
    with pytest.raises(ValidationError):
        Keuze(apparaat='accu', stand='laden', minuten=1441)


def test_runtime_expiry_resets_mode_persists_and_requests_new_plan(tmp_path):
    from brein.brein import Brein
    from types import SimpleNamespace
    b = Brein.__new__(Brein)
    b.bed = Bediening()
    b.bed.kies('accu', 'laden', 30, NOW - timedelta(days=1))
    b._bed_pad = tmp_path / 'bediening.json'
    events = []
    b.logboek = SimpleNamespace(schrijf=lambda *args: events.append(args))
    b.herplan = lambda: events.append('replan')
    b._verloop_bediening()
    assert b.bed.accu == 'auto'
    assert json.loads(b._bed_pad.read_text())['tot'] == {}
    assert events[-1] == 'replan'


def test_period_total_uses_wear_setting_and_handles_old_rows():
    from types import SimpleNamespace
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [row()]
    rows[0]['start'] = start.isoformat()
    g = SimpleNamespace(rijen=lambda *_: rows)
    total = kosten_periode(g, Prices(), 'maand', 10, 1, .1)['totaal']
    assert total['slijtage_eur'] == .2
    rows[0]['accu_geladen_kwh'] = None
    total = kosten_periode(g, Prices(), 'maand', 10, 1, .1)['totaal']
    assert total['door_accu_netto'] is None

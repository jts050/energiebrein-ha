import copy
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'energiebrein/app'))
from brein.config import laad
from brein.model import Kwartier, Bediening
from brein.planner import Planner, AutoStatus

def setup(load=.1, n=96, enabled=True, solar=0):
    cfg=copy.deepcopy(laad())
    cfg['accu'].update(capaciteit_kwh=25, min_soc=10, max_soc=90,
        max_laden_kw=8, max_ontladen_kw=8, rendement=.86,
        eigen_huis_eerst=enabled, onzekerheidsreserve=False,
        terugverkopen=True, zon_altijd_in_accu=False)
    cfg['warmtepomp']['type']='uit'
    p=Planner(cfg)
    start=datetime(2026,11,6,16,tzinfo=timezone.utc)
    ks=[Kwartier(start+timedelta(minutes=15*i), .2,
        .8 if i<16 else .35, .75 if i<16 else .3,
        huis_kwh=load, zon_kwh=solar) for i in range(n)]
    return p,ks

def plan(p,ks,soc=90):
    return p.maak(ks,soc,Bediening(),AutoStatus(False),lambda _:0)

def exports(ks):
    return sum(max(0,-k.net_kwh) for k in ks if k.accu_kwh<0)

def test_reserve_stops_sale_of_needed_evening_and_night_energy():
    p,ks=setup(load=.2)
    plan(p,ks)
    assert exports(ks)==pytest.approx(0)
    assert any(k.accu_kwh<0 for k in ks)  # protected stock feeds the house
    assert min(k.soc_na for k in ks)<40

def test_abundant_charge_can_still_sell_only_above_reserve():
    p,ks=setup(load=.025)
    plan(p,ks)
    assert exports(ks)>1
    for k in ks:
        if k.accu_kwh<0 and k.net_kwh<0:
            assert (k.soc_na-10)*.25 >= p.verkoopreserves[k.start]-1e-7

def test_switch_off_preserves_unrestricted_trading():
    p,ks=setup(load=.2,enabled=False)
    plan(p,ks)
    assert exports(ks)>1
    assert not p.verkoopreserves and not p.eigen_reserve_info['actief']

def test_sun_tomorrow_cannot_pay_for_tonights_deficit():
    p,ks=setup(load=.1)
    for k in ks[64:80]:k.zon_kwh=2
    p._verkoopreserve(ks)
    assert p.verkoopreserves[ks[0].start]>=63*.12/p._eta()-1e-7

def test_heat_pump_and_car_increase_reserve_once():
    p,ks=setup(load=.02)
    p._verkoopreserve(ks)
    before=p.verkoopreserves[ks[0].start]
    ks[8].wp_kwh=1
    ks[8].water_kwh=1  # already included in wp_kwh
    ks[9].auto_kwh=1
    p._verkoopreserve(ks)
    assert p.verkoopreserves[ks[0].start]-before==pytest.approx(2.2/p._eta())

def test_prediction_errors_and_losses_increase_reserve():
    p,ks=setup(load=.02,solar=.01)
    p._verkoopreserve(ks)
    before=p.verkoopreserves[ks[0].start]
    p.onzekerheid={'huis':.5,'zon':.6}
    p.rendement=.7
    p._verkoopreserve(ks)
    assert p.verkoopreserves[ks[0].start]>before

def test_below_reserve_no_fictional_charge_and_hardware_floor_honoured():
    p,ks=setup(load=.2)
    p.ondergrens=15
    plan(p,ks,soc=20)
    assert p._e_start==pytest.approx(5,abs=.05)
    assert min(k.soc_na for k in ks)>=15-1e-7
    assert exports(ks)==0

def test_short_horizon_preserves_stock_beyond_last_known_price():
    p,ks=setup(load=.1,n=4)
    p._verkoopreserve(ks)
    assert p.verkoopreserves[ks[-1].start]==pytest.approx(96*.12/p._eta())

def test_live_limit_responds_to_soc_and_current_house_load():
    p,ks=setup(load=.025)
    p._verkoopreserve(ks)
    k=ks[0]
    reserve=p.verkoopreserves[k.start]
    soc=10+(reserve+1)/25*100
    assert p.verkoopruimte_kw(k,soc,1)==pytest.approx(4*p._eta()-1)
    assert p.verkoopruimte_kw(k,10,1)==0
    assert p.verkoopruimte_kw(k,soc,10)==0
    p.verkoopreserves.clear()
    assert p.verkoopruimte_kw(k,90,0)==0

def test_recalculation_clears_previous_reserve_when_disabled():
    p,ks=setup()
    p._verkoopreserve(ks)
    p.cfg['accu']['eigen_huis_eerst']=False
    p._verkoopreserve(ks)
    assert not p.verkoopreserves and not p.eigen_reserve_info['actief']

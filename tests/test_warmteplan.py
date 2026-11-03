import asyncio
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'energiebrein/app'))
from brein.model import Kwartier, Bediening
from brein.planner import Planner
from brein.warmteplan import goedkoop_blok, plan_warmwater, warmteblokken
from brein.warmteplan import avondprofiel
from brein.warmwater import WarmwaterRegelaar
from brein.warmte import overzicht
from brein.config import laad

def kwartieren():
    begin = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return [Kwartier(begin + timedelta(minutes=15*i), .2, .4 if i < 12 else .2, .1,
                     huis_kwh=.1) for i in range(96)]

def test_no_gaps_or_unknown_prices():
    ks = kwartieren()[:8]
    assert len(goedkoop_blok(ks)) == 4
    for k in ks:k.inkoop = float('nan')
    assert goedkoop_blok(ks) == []
    ks = kwartieren()[::2]
    assert goedkoop_blok(ks) == []

def test_water_deadline_energy_and_stable_choice():
    ks = kwartieren()
    cfg = {'type':'ha_thermostaat', 'warmwater_slim':True,
           'comfort_tijden':['06:00-08:30'], 'warmwater_extra_kwh':1.2}
    keuzes = {}
    plan_warmwater(ks, cfg, keuzes)
    gekozen = [k for k in ks if k.water_boost]
    assert len(gekozen) == 4
    assert max(k.start for k in gekozen) + timedelta(minutes=15) <= ks[24].start
    assert abs(sum(k.wp_kwh for k in ks) - 1.2) < 1e-9
    nieuw = [Kwartier(k.start, .2, .8, .1) for k in ks[16:]]
    plan_warmwater(nieuw, cfg, keuzes)
    assert {k.start for k in nieuw if k.water_boost} == {k.start for k in gekozen if k.start >= nieuw[0].start}
    blokken = warmteblokken(ks, cfg)
    assert len(blokken) == 1 and blokken[0]['soort'] == 'water'
    assert abs(blokken[0]['kwh'] - 1.2) < 1e-9

def test_water_off_or_meter_never_schedules():
    for cfg in [{'type':'meter','warmwater_slim':True}, {'type':'ha_thermostaat'}]:
        ks=kwartieren();plan_warmwater(ks,cfg,{})
        assert not any(k.water_boost for k in ks)

def test_room_one_contiguous_hour_no_setback_and_fixed_during_replan():
    cfg=copy.deepcopy(laad());cfg['warmtepomp'].update(type='ha_thermostaat',rustig_voorverwarmen=True)
    p=Planner(cfg);ks=kwartieren();p._warmtepomp(ks,Bediening(),lambda t:8)
    boost=[k.start for k in ks if k.wp_stand=='boost']
    assert len(boost)==4 and boost[-1]-boost[0]==timedelta(minutes=45)
    assert not any(k.wp_stand=='wacht' for k in ks)
    nieuw=kwartieren()[14:];p._warmtepomp(nieuw,Bediening(),lambda t:8)
    assert {k.start for k in nieuw if k.wp_stand=='boost'} == {t for t in boost if t>=nieuw[0].start}

class NepWater(WarmwaterRegelaar):
    def __init__(self,pad=None):
        super().__init__('climate.atag',pad)
        self.target=55;self.mode='Always active';self.commands=[]
    async def lees(self):
        return overzicht([{'entity_id':'water_heater.atag','state':self.mode,'attributes':{
            'temperature':self.target,'current_temperature':53,'min_temp':35,'max_temp':65,
            'target_temp_step':1,'supported_features':3}}],'climate.atag')
    async def zet(self,data,doel):
        self.commands.append(doel);self.target=doel

def test_water_restore_restart_and_manual_override(tmp_path):
    async def run():
        nu=datetime.now(timezone.utc);tot=(nu+timedelta(hours=1)).isoformat()
        pad=tmp_path/'water.json';w=NepWater(pad)
        await w.regel(tot,nu);assert w.commands==[57]
        restored=NepWater(pad);restored.target=57
        await restored.regel(None,nu+timedelta(hours=1));assert restored.commands==[55]
        await w.regel(tot,nu);w.target=56
        await w.regel(tot,nu);await w.regel(None,nu+timedelta(hours=1))
        assert w.target==56
    asyncio.run(run())

def test_disabled_green_and_legionella_never_overridden():
    async def run():
        nu=datetime.now(timezone.utc);tot=(nu+timedelta(hours=1)).isoformat()
        for mode in ['Disabled','Green Mode','Time Based','unavailable']:
            w=NepWater();w.mode=mode
            try:await w.regel(tot,nu)
            except RuntimeError:pass
            assert not w.commands
        w=NepWater();w.target=65;await w.regel(tot,nu);assert not w.commands
        w.target=55;await w.regel(tot,nu);w.target=65
        await w.regel(None,nu+timedelta(hours=1));assert w.target==65 and w.commands==[57]
    asyncio.run(run())

def test_translates_real_sensor_names_without_faking_unknown():
    rows=[{'entity_id':'sensor.atag_ariston_ch_flow_temp','state':'22.0','attributes':{}},
          {'entity_id':'sensor.atag_ariston_gas_consumption_for_water_last_month','state':'0','attributes':{}},
          {'entity_id':'sensor.atag_ariston_ch_return_temp','state':'unavailable','attributes':{}}]
    result=overzicht(rows,'climate.atag',gasloos=True)['entiteiten']
    assert [r['naam'] for r in result]==['Cv-aanvoertemperatuur','Cv-retourtemperatuur']
    assert result[1]['waarde']=='unavailable'

def test_evening_profile_ignores_gaps_and_estimated_values():
    rows=[];begin=kwartieren()[0].start
    for d in range(3):
        for q in range(64,96):
            t=begin+timedelta(days=d,minutes=15*q)
            rows.append({'start':t.isoformat(),'wp_meetseconden':900,
                         'wp_gemeten_kwh':.8 if t.hour==19 else .1})
    p=avondprofiel(rows)
    assert p['voldoende'] and p['piek_uur']==19 and abs(p['piek_kw']-3.2)<1e-9
    assert not avondprofiel(rows[:-1])['voldoende']
    assert not avondprofiel([dict(r,wp_meetseconden=None,wp_kwh=10) for r in rows])['voldoende']

def test_measured_evening_peak_selects_preheat_before_peak():
    cfg=copy.deepcopy(laad());cfg['warmtepomp'].update(type='ha_thermostaat',rustig_voorverwarmen=True)
    p=Planner(cfg);p.avondprofiel={'voldoende':True,'piek_uur':19}
    ks=kwartieren();p._warmtepomp(ks,Bediening(),lambda t:8)
    boost=[k for k in ks if k.wp_stand=='boost']
    assert len(boost)==4 and all(15<=k.start.astimezone().hour<19 for k in boost)

def test_arrival_overrides_evening_peak_and_model_is_no_bill_claim():
    from brein.warmteplan import prijsvoordeel
    cfg=copy.deepcopy(laad());cfg['warmtepomp'].update(type='ha_thermostaat',rustig_voorverwarmen=True,thuiskomst='16:00')
    p=Planner(cfg);p.avondprofiel={'voldoende':True,'piek_uur':20}
    ks=kwartieren();p._warmtepomp(ks,Bediening(),lambda t:8)
    boost=[k for k in ks if k.wp_stand=='boost']
    assert len(boost)==4 and all(12<=k.start.astimezone().hour<16 for k in boost)
    v=prijsvoordeel(ks,lambda t:8,cfg['warmtepomp'])
    assert abs(v['prijsvoordeel_huis']-(v['kosten_normaal']-v['kosten_gepland_huis']))<1e-9
    assert 'geen bewezen besparing' in v['uitleg']
    assert prijsvoordeel(ks,lambda t:8,{'type':'uit'})=={}

def test_thermostat_limits_native_steps_and_respects_off_and_manual_choice():
    from brein.apparaten.echt import HAThermostaat
    class NepThermostaat(HAThermostaat):
        def __init__(self):
            super().__init__({'warmtepomp':{'thermostaat':'climate.atag','rustig_voorverwarmen':True,'verhoging':2}})
            self.target=21;self.mode='heat';self.step=.5;self.commands=[]
        async def _toestand(self):
            return {'state':self.mode,'attributes':{'temperature':self.target,'target_temp_step':self.step}}
        async def _zet(self,value,t):
            self.commands.append(value);self.target=value;return value
    async def run():
        w=NepThermostaat();await w.warmtepomp('boost');assert w.target==21.5
        w.target=22;await w.warmtepomp('normaal');assert w.target==22
        w=NepThermostaat();w.mode='off';await w.warmtepomp('boost');assert not w.commands
        w=NepThermostaat();w.step=1;await w.warmtepomp('boost');assert w.target==21
        w=NepThermostaat();await w.warmtepomp('wacht');assert w.target==21
    asyncio.run(run())

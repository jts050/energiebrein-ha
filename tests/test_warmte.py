import importlib.util, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('warmte',Path(__file__).parents[1]/'energiebrein/app/brein/warmte.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
class WarmteTest(unittest.TestCase):
 def setUp(self):
  self.states=[{'entity_id':'climate.atag','state':'heat','attributes':{'current_temperature':20.5,'temperature':21,'min_temp':10,'max_temp':30,'target_temp_step':0.5,'supported_features':1}}, {'entity_id':'water_heater.atag','state':'Always active','attributes':{'current_temperature':53,'temperature':55,'min_temp':35,'max_temp':65,'target_temp_step':1,'supported_features':3}}, {'entity_id':'sensor.atag_outside','state':'8','attributes':{}},{'entity_id':'sensor.atag_other_secret','state':'3','attributes':{}},{'entity_id':'sensor.other','state':'9','attributes':{}}]
  self.data=w.overzicht(self.states,'climate.atag')
 def test_water_and_house_are_independent(self):
  self.assertEqual(self.data['water']['huidig'],53);self.assertEqual(self.data['huis']['doel'],21)
  self.assertEqual(w.controleer_temperatuur(self.data,'water',55),('water_heater.atag',55))
  self.assertEqual(w.controleer_temperatuur(self.data,'huis',20.5),('climate.atag',20.5))
 def test_invalid_temperature_never_reaches_service(self):
  for value in [34,66,float('nan'),float('inf'),55.5]:
   with self.subTest(value=value),self.assertRaises(ValueError):w.controleer_temperatuur(self.data,'water',value)
  with self.assertRaises(ValueError):w.controleer_temperatuur(self.data,'other',55)
 def test_missing_and_unavailable_are_not_zero(self):
  data=w.overzicht(self.states[:1],'climate.atag');self.assertIsNone(data['water'])
  self.states[1]['state']='unavailable';data=w.overzicht(self.states,'climate.atag')
  self.assertIsNone(data['water']['huidig'])
  with self.assertRaises(ValueError):w.controleer_temperatuur(data,'water',55)
 def test_supported_features_required(self):
  self.states[1]['attributes']['supported_features']=2
  with self.assertRaises(ValueError):w.controleer_temperatuur(w.overzicht(self.states,'climate.atag'),'water',55)
 def test_no_unrelated_entities_or_arbitrary_targets(self):
  self.assertNotIn('sensor.other',[e['entity'] for e in self.data['entiteiten']])
  for entity in ['switch.atag','climate.atag/path','climate.atag?foo=1']:
   with self.assertRaises(ValueError):w.entiteiten(entity)
if __name__=='__main__':unittest.main()

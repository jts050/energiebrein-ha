import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'energiebrein/app'))
import goodwe
from brein import zoeken


def test_zoeker_bevestigt_goodwe_ook_bij_andere_modulenaam(monkeypatch):
    async def search():
        return b'192.168.2.77,AABB,ANDERE-SSID'
    async def connect(ip, **kwargs):
        assert ip == '192.168.2.77'
        return type('Inv', (), {'model_name': 'GW8K-ET'})()
    monkeypatch.setattr(goodwe, 'search_inverters', search)
    monkeypatch.setattr(goodwe, 'connect', connect)
    resultaat = asyncio.run(zoeken._udp_dongles())
    assert resultaat[0]['merk'] == 'GoodWe'
    assert resultaat[0]['koppel'] == {'omvormer': 'goodwe'}


def test_onbevestigde_wifi_module_krijgt_geen_verkeerd_merk(monkeypatch):
    async def search():
        return b'192.168.2.77,AABB,Solar-WiFi'
    async def connect(*args, **kwargs):
        raise OSError('geen antwoord')
    monkeypatch.setattr(goodwe, 'search_inverters', search)
    monkeypatch.setattr(goodwe, 'connect', connect)
    resultaat = asyncio.run(zoeken._udp_dongles())
    assert resultaat[0]['ip'] == '192.168.2.77'
    assert resultaat[0]['koppel'] is None
    assert resultaat[0]['merk'] == 'Wifi-module (niet herkend)'

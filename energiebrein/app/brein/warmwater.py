"""Begrensde tijdelijke warmwaterinstelling; de eigen ATAG-regeling blijft actief."""
import json
from datetime import datetime, timezone
import aiohttp
from .warmte import overzicht, controleer_temperatuur


class WarmwaterRegelaar:
    def __init__(self, thermostaat, pad=None):
        self.thermostaat, self.pad = thermostaat, pad
        self.staat = {}
        if pad and pad.exists():
            # Een beschadigd herstelbestand vraagt om aandacht, geen blinde
            # temperatuurwijziging. Laat de constructor de fout doorgeven.
            self.staat = json.loads(pad.read_text(encoding='utf-8'))
            if not isinstance(self.staat, dict):
                raise ValueError('Warmwater-herstelbestand is ongeldig')

    def bewaar(self):
        if self.pad:
            tijdelijk = self.pad.with_suffix('.tmp')
            tijdelijk.write_text(json.dumps(self.staat), encoding='utf-8')
            tijdelijk.replace(self.pad)

    async def lees(self):
        from . import telefoon
        if not telefoon.beschikbaar():
            raise RuntimeError('Warmwatersturing vereist Home Assistant')
        entity = 'water_heater.' + self.thermostaat.split('.', 1)[1]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f'{telefoon.HA}/states/{entity}', headers=telefoon._kop()) as r:
                r.raise_for_status()
                return overzicht([await r.json()], self.thermostaat)

    async def zet(self, data, doel):
        from . import telefoon
        entity, waarde = controleer_temperatuur(data, 'water', doel)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(f'{telefoon.HA}/services/water_heater/set_temperature',
                    headers=telefoon._kop(), json={'entity_id': entity, 'temperature': waarde}) as r:
                r.raise_for_status()

    async def regel(self, tot=None, nu=None):
        nu = nu or datetime.now(timezone.utc)
        actief = bool(tot and datetime.fromisoformat(tot) > nu)
        if not actief and not self.staat.get('basis'):
            return
        data = await self.lees()
        water = data.get('water')
        if not water or not water['instelbaar'] or water['doel'] is None:
            raise RuntimeError('Warmwatertemperatuur tijdelijk niet beschikbaar; geen wijziging verstuurd')
        huidig = water['doel']
        # Alleen de aantoonbaar actieve native regeling ondersteunen, geen timer
        # omzeilen, geen Green/HC-HP of uitgeschakelde boiler inschakelen.
        toegestaan = str(water['stand']).lower() == 'always active'
        if self.staat.get('basis') is not None:
            zelfde = abs(huidig - self.staat['gezet']) < .01
            if actief and huidig == self.staat['basis'] and not self.staat.get('bevestigd'):
                # De cloud kan de oude waarde nog melden. Niet opnieuw verhogen
                # en de herstelwaarde behouden tot een bevestigde meting.
                return
            if not zelfde or not toegestaan:
                # Handmatige wijziging of legionellacyclus heeft altijd voorrang.
                self.staat = {'overgeslagen': self.staat.get('tot')}
                self.bewaar()
                return
            if not self.staat.get('bevestigd'):
                self.staat['bevestigd'] = True
                self.bewaar()
            if not actief or tot != self.staat['tot']:
                await self.zet(data, self.staat['basis'])
                self.staat = {}
                self.bewaar()
            return
        if not actief or not toegestaan or self.staat.get('overgeslagen') == tot:
            return
        # Verhoog nooit een al hoge desinfectie-instelling; verlaag die ook niet.
        if huidig >= 60 or water['max'] is None:
            return
        stap = water['stap']
        doel = min(huidig + 2, 60, water['max'])
        doel = huidig + int((doel - huidig) / stap) * stap
        if doel <= huidig:
            return
        controleer_temperatuur(data, 'water', doel)
        self.staat = {'basis': huidig, 'gezet': doel, 'tot': tot}
        self.bewaar()  # na een herstart kan de eigen instelling veilig terug
        await self.zet(data, doel)

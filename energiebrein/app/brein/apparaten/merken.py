"""Koppelingen per merk.

Bronnen per merk staan bij elke klasse. Waar een bewezen bibliotheek bestaat (dezelfde als Home Assistant gebruikt)
gebruiken we die. Registeradressen van Victron, Sungrow, SolarEdge, SMA en Fronius komen uit de documentatie
van de fabrikant zoals ook gebruikt door evcc (open source energiebeheer).

Veiligheid bij sturen:
- Sturen staat per merk standaard UIT (config: omvormer.sturen). Zonder die schakelaar wordt nooit iets geschreven.
- Vóór de eerste opdracht controleert de driver of het apparaat antwoordt zoals verwacht; anders weigert hij.
- Een opdracht wordt alleen geschreven als hij verandert (en hooguit eens per minuut herhaald).
- Bij stoppen zet de driver het apparaat terug in de stand waarin hij het aantrof.
- Opdrachten die een merk niet kent, worden 'zelfverbruik' (de normale stand van de omvormer).
"""
from __future__ import annotations
import asyncio
import json
import json as _json
import logging
import math
import struct
import time
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("merken")
ALLE_ACTIES = {"laden", "ontladen", "vasthouden", "zelfverbruik"}


class NietToegestaan(NotImplementedError):
    pass


# ============================================================ hulp: Modbus TCP
class Modbus:
    def __init__(self, host: str, poort: int = 502, unit: int = 1):
        self.host, self.poort, self.unit = host, poort, unit
        self.c = None

    async def open(self):
        from pymodbus.client import AsyncModbusTcpClient
        if self.c and self.c.connected:
            return
        self.c = AsyncModbusTcpClient(self.host, port=self.poort, timeout=3)
        if not await self.c.connect():
            raise ConnectionError(f"Modbus op {self.host}:{self.poort} niet bereikbaar. Staat Modbus TCP aan op het apparaat?")

    def sluit(self):
        if self.c:
            self.c.close()

    async def lees(self, adres: int, aantal: int = 1, soort: str = "holding", unit: int | None = None) -> list[int]:
        await self.open()
        f = self.c.read_holding_registers if soort == "holding" else self.c.read_input_registers
        r = await f(adres, count=aantal, device_id=self.unit if unit is None else unit)
        if r.isError():
            raise IOError(f"Register {adres} lezen mislukt ({r})")
        return list(r.registers)

    async def schrijf(self, adres: int, waarden: list[int], unit: int | None = None):
        await self.open()
        u = self.unit if unit is None else unit
        r = (await self.c.write_register(adres, waarden[0], device_id=u)) if len(waarden) == 1 else \
            (await self.c.write_registers(adres, waarden, device_id=u))
        if r.isError():
            raise IOError(f"Register {adres} schrijven mislukt ({r})")


def s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def u32(regs, woordwissel=False) -> int:
    hi, lo = (regs[1], regs[0]) if woordwissel else (regs[0], regs[1])
    return (hi << 16) | lo


def s32(regs, woordwissel=False) -> int:
    v = u32(regs, woordwissel)
    return v - (1 << 32) if v >= (1 << 31) else v


def f32_ww(regs) -> float:
    """float32 met woordwissel (SolarEdge 'float32s')."""
    v = struct.unpack(">f", struct.pack(">HH", regs[1], regs[0]))[0]
    return 0.0 if math.isnan(v) else v


def naar_f32_ww(x: float) -> list[int]:
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(x)))
    return [lo, hi]


def naar_u32(x: int) -> list[int]:
    x = int(x) & 0xFFFFFFFF
    return [x >> 16, x & 0xFFFF]


# ============================================================ basis voor accu-omvormers
class AccuOmvormer:
    acties = ALLE_ACTIES
    kan_aftoppen = False

    def __init__(self, cfg: dict):
        self.o = cfg["omvormer"]
        self.accu_cfg = cfg["accu"]
        self.sturen_aan = bool(self.o.get("sturen", False))
        self._laatste = None
        self._laatst_geschreven = 0.0

    @property
    def kan_sturen(self) -> bool:
        return self.sturen_aan

    def _nodig(self, opdracht) -> bool:
        """Alleen schrijven bij verandering, of eens per minuut ter bevestiging."""
        if opdracht != self._laatste or time.monotonic() - self._laatst_geschreven > 60:
            self._laatste, self._laatst_geschreven = opdracht, time.monotonic()
            return True
        return False

    async def accu(self, actie: str, kw: float = 0.0, doel_soc: float = 100.0):
        if not self.sturen_aan:
            raise NietToegestaan("Sturen staat uit voor dit merk. Zet het aan bij Koppelen zodra het uitlezen klopt.")
        if actie not in self.acties:
            actie = "zelfverbruik"
        await self._stuur(actie, max(0.0, kw), doel_soc)

    async def exportlimiet(self, kw):
        if kw == 0 and not self.kan_aftoppen:
            raise NietToegestaan("Aftoppen is voor dit merk niet gekoppeld.")
        if kw == 0 and not self.sturen_aan:
            raise NietToegestaan("Aftoppen kan pas als sturen aan staat.")
        if self.kan_aftoppen and self.sturen_aan and (kw == 0 or self._laatste):
            await self._aftop(kw == 0)

    async def _stuur(self, actie, kw, doel):
        raise NotImplementedError

    async def _aftop(self, aan: bool):
        raise NotImplementedError


# ============================================================ Huawei SUN2000 + LUNA2000
class Huawei(AccuOmvormer):
    """Via de bibliotheek 'huawei-solar' (ook gebruikt door Home Assistant). Modbus TCP via SDongle (poort 502)
    of de interne wifi (poort 6607). Schrijven vraagt vaak het installateurswachtwoord."""
    kan_aftoppen = True

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.dev = None
        self._apc_origineel = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 502), "modbus_unit": extra.get("modbus_unit", 1)}, "accu": {}})

    async def start(self):
        import huawei_solar as hs
        client = hs.create_tcp_client(self.o["host"], int(self.o.get("modbus_poort", 502)), unit_id=int(self.o.get("modbus_unit", 1)))
        self.dev = await hs.create_device_instance(client)
        if self.o.get("wachtwoord"):
            await self.dev.login("installer", self.o["wachtwoord"])
        try:
            from huawei_solar import register_names as rn
            r = await self.dev.batch_update([rn.ACTIVE_POWER_CONTROL_MODE])
            self._apc_origineel = r[rn.ACTIVE_POWER_CONTROL_MODE].value
        except Exception:
            self._apc_origineel = None

    async def stop(self):
        if self.dev and self.sturen_aan and self._laatste:
            try:
                await self._stuur("zelfverbruik", 0, 100)
                if self._apc_origineel is not None:
                    from huawei_solar import register_names as rn
                    await self.dev.set(rn.ACTIVE_POWER_CONTROL_MODE, self._apc_origineel)
            except Exception as e:
                log.warning("Huawei terugzetten mislukt: %s", e)

    async def lees(self) -> dict:
        from huawei_solar import register_names as rn
        if self.dev is None:
            await self.start()
        r = await self.dev.batch_update([rn.INPUT_POWER, rn.STORAGE_STATE_OF_CAPACITY, rn.STORAGE_CHARGE_DISCHARGE_POWER, rn.POWER_METER_ACTIVE_POWER])
        uit = {"zon_kw": (r[rn.INPUT_POWER].value or 0) / 1000,
               "accu_soc": float(r[rn.STORAGE_STATE_OF_CAPACITY].value or 0),
               "accu_kw": (r[rn.STORAGE_CHARGE_DISCHARGE_POWER].value or 0) / 1000}   # Huawei: + = laden
        if r[rn.POWER_METER_ACTIVE_POWER].value is not None:
            uit["net_kw"] = -r[rn.POWER_METER_ACTIVE_POWER].value / 1000          # Huawei-meter: + = teruglevering
        return uit

    async def _stuur(self, actie, kw, doel):
        from huawei_solar import register_names as rn
        from huawei_solar.register_values import StorageForcibleChargeDischarge as F, StorageForcibleChargeDischargeTargetMode as M
        w = int(kw * 1000)
        if actie == "laden":
            opdracht = ("laden", round(w, -2), int(doel))
        elif actie == "ontladen":
            opdracht = ("ontladen", round(w, -2), int(self.accu_cfg.get("min_soc", 10)))
        else:   # vasthouden kent Huawei niet als stand; zelfverbruik is de normale stand
            opdracht = ("stop",)
        if not self._nodig(opdracht):
            return
        if opdracht[0] == "stop":
            await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE, F.STOP)
        else:
            await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE, M.SOC)
            await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC, opdracht[2])
            if opdracht[0] == "laden":
                await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_POWER, opdracht[1])
                await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE, F.CHARGE)
            else:
                await self.dev.set(rn.STORAGE_FORCIBLE_DISCHARGE_POWER, opdracht[1])
                await self.dev.set(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE, F.DISCHARGE)
        log.info("Huawei: %s", opdracht)

    async def _aftop(self, aan):
        from huawei_solar import register_names as rn
        from huawei_solar.register_values import ActivePowerControlMode as A
        doel = A.ZERO_POWER_GRID_CONNECTION if aan else (self._apc_origineel or A.UNLIMITED)
        if self._nodig(("aftop", aan)):
            await self.dev.set(rn.ACTIVE_POWER_CONTROL_MODE, doel)


# ============================================================ Victron GX (ESS)
class Victron(AccuOmvormer):
    """Modbus TCP op de GX (unit 100 = com.victronenergy.system). Lezen: 808-813 en 850 (zon), 820-822 (net),
    842 (accu, + = laden), 843 (lading). Sturen via ESS-netsetpoint (register 2700, watt, + = afname).
    Vereist: ESS-assistent en 'Modbus TCP' aan op de GX."""
    acties = ALLE_ACTIES

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.mb = Modbus(self.o["host"], int(self.o.get("modbus_poort", 502)), 100)
        self._origineel = None
        self._net = self._accu = 0.0

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 502)}, "accu": {}})

    async def start(self):
        await self.mb.open()
        self._origineel = s16((await self.mb.lees(2700))[0])

    async def stop(self):
        if self.sturen_aan and self._origineel is not None and self._laatste:
            try:
                await self.mb.schrijf(2700, [self._origineel & 0xFFFF])
            except Exception as e:
                log.warning("Victron terugzetten mislukt: %s", e)
        self.mb.sluit()

    async def _r(self, adres, n):
        try:
            return await self.mb.lees(adres, n)
        except IOError:
            return await self.mb.lees(adres, n, "input")

    async def lees(self) -> dict:
        zon = sum(await self._r(808, 6)) + (await self._r(850, 1))[0]
        net = sum(s16(v) for v in await self._r(820, 3))
        accu = s16((await self._r(842, 1))[0])
        soc = (await self._r(843, 1))[0]
        self._net, self._accu = net, accu
        return {"zon_kw": zon / 1000, "net_kw": net / 1000, "accu_kw": accu / 1000, "accu_soc": float(soc)}

    async def _stuur(self, actie, kw, doel):
        # netsetpoint = wat het huis nu van het net vraagt zonder accu + gewenste accu-stroom
        zonder_accu = self._net - self._accu
        doelw = {"laden": kw * 1000, "ontladen": -kw * 1000, "vasthouden": 0.0}.get(actie)
        setpoint = int(self._origineel or 0) if doelw is None else int(zonder_accu + doelw)
        setpoint = max(-32000, min(32000, setpoint))
        vorig = self._laatste[1] if self._laatste else None
        if vorig is not None and self._laatste[0] == actie and abs(vorig - setpoint) < 100 and time.monotonic() - self._laatst_geschreven < 60:
            return
        self._laatste, self._laatst_geschreven = (actie, setpoint), time.monotonic()
        await self.mb.schrijf(2700, [setpoint & 0xFFFF])
        log.info("Victron: netsetpoint %s W (%s)", setpoint, actie)


# ============================================================ Sungrow SH (WiNet-S of LAN)
class Sungrow(AccuOmvormer):
    """Modbus TCP, unit 1. Lezen (input): 5016 zon (u32, woordwissel), 13009 teruglevering (s32, woordwissel),
    13000/13020/13021 accu, 13022 lading (×0,1). Sturen (holding): 13049 EMS-stand (0 zelfverbruik, 2 geforceerd),
    13050 opdracht (0xAA laden, 0xBB ontladen, 0xCC stop), 13051 vermogen (W), 33046/33047 max laden/ontladen (×10 W)."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.mb = Modbus(self.o["host"], int(self.o.get("modbus_poort", 502)), int(self.o.get("modbus_unit", 1)))
        self._orig = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 502), "modbus_unit": extra.get("modbus_unit", 1)}, "accu": {}})

    async def start(self):
        await self.mb.open()
        if self.sturen_aan:
            ems = (await self.mb.lees(13049))[0]
            if ems not in (0, 2):   # controle: klopt de registerkaart voor dit model?
                self.sturen_aan = False
                raise ConnectionError(f"Onverwachte EMS-stand ({ems}); sturen uitgezet om veilig te blijven.")
            self._orig = await self.mb.lees(33046, 2)

    async def stop(self):
        if self.sturen_aan and self._laatste:
            try:
                await self.mb.schrijf(13049, [0])
                await self.mb.schrijf(13050, [0xCC])
                if self._orig:
                    await self.mb.schrijf(33046, self._orig)
            except Exception as e:
                log.warning("Sungrow terugzetten mislukt: %s", e)
        self.mb.sluit()

    async def lees(self) -> dict:
        zon = u32(await self.mb.lees(5016, 2, "input"), True)
        export = s32(await self.mb.lees(13009, 2, "input"), True)
        stand, stroom, vermogen, soc = (await self.mb.lees(13000, 1, "input"))[0], *(await self.mb.lees(13020, 3, "input"))
        stroom, vermogen = s16(stroom), s16(vermogen)
        ontladen = vermogen
        if ((stand & 0x2) or stroom < 0) and vermogen >= 0:
            ontladen = -vermogen
        return {"zon_kw": zon / 1000, "net_kw": -export / 1000, "accu_kw": -ontladen / 1000, "accu_soc": s16(soc) / 10}

    async def _stuur(self, actie, kw, doel):
        w = int(round(kw * 1000, -2))
        opdracht = (actie, w)
        if not self._nodig(opdracht):
            return
        maxl, maxo = (self._orig or [500, 500])
        if actie == "laden":
            await self.mb.schrijf(13049, [2]); await self.mb.schrijf(13050, [0xAA]); await self.mb.schrijf(13051, [w])
        elif actie == "ontladen":
            await self.mb.schrijf(13049, [2]); await self.mb.schrijf(13050, [0xBB]); await self.mb.schrijf(13051, [w])
        elif actie == "vasthouden":
            await self.mb.schrijf(13049, [0]); await self.mb.schrijf(13050, [0xCC]); await self.mb.schrijf(33046, [maxl, 1])
        else:
            await self.mb.schrijf(13049, [0]); await self.mb.schrijf(13050, [0xCC]); await self.mb.schrijf(33046, [maxl, maxo])
        log.info("Sungrow: %s %s W", actie, w)


# ============================================================ SolarEdge (StorEdge, Modbus TCP)
class SolarEdge(AccuOmvormer):
    """Modbus TCP (vaak poort 1502), unit 1. Zon: SunSpec-omvormermodel. Accu: 0xE174 vermogen (float, + = laden),
    0xE184 lading. Sturen: alleen als StorageConf_CtrlMode (0xE004) op 'Remote' (4) staat (SetApp / installateur).
    Opdracht 0xE00D: 3 laden uit zon+net, 4 maximaal leveren, 7 zelfverbruik. Grenzen: 0xE00E laden, 0xE010 ontladen (W)."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.mb = Modbus(self.o["host"], int(self.o.get("modbus_poort", 1502)), int(self.o.get("modbus_unit", 1)))
        self.adres_w = None
        self.max_laden = self.max_ontladen = 5000.0

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 1502), "modbus_unit": extra.get("modbus_unit", 1)}, "accu": {}})

    async def start(self):
        await self.mb.open()
        self.adres_w = await _sunspec_model(self.mb, (101, 102, 103), 12)
        if self.sturen_aan:
            mode = (await self.mb.lees(0xE004))[0]
            if mode != 4:
                self.sturen_aan = False
                raise ConnectionError("Sturen kan pas als 'Storage Control Mode' op Remote staat (via SetApp of je installateur).")
            self.max_laden = f32_ww(await self.mb.lees(0xE144, 2)) or 5000.0
            self.max_ontladen = f32_ww(await self.mb.lees(0xE146, 2)) or 5000.0

    async def stop(self):
        if self.sturen_aan and self._laatste:
            try:
                await self._schrijf(7, self.max_laden, self.max_ontladen)
            except Exception as e:
                log.warning("SolarEdge terugzetten mislukt: %s", e)
        self.mb.sluit()

    async def lees(self) -> dict:
        w, sf = await self.mb.lees(self.adres_w, 2)
        zon = max(s16(w) * 10 ** s16(sf), 0)
        accu = f32_ww(await self.mb.lees(0xE174, 2))
        soc = f32_ww(await self.mb.lees(0xE184, 2))
        return {"zon_kw": zon / 1000, "accu_kw": accu / 1000, "accu_soc": soc}

    async def _schrijf(self, opdracht, laden_w, ontladen_w):
        await self.mb.schrijf(0xE00D, [opdracht])
        await self.mb.schrijf(0xE00E, naar_f32_ww(laden_w))
        await self.mb.schrijf(0xE010, naar_f32_ww(ontladen_w))

    async def _stuur(self, actie, kw, doel):
        w = min(kw * 1000, self.max_laden if actie == "laden" else self.max_ontladen)
        opdracht = {"laden": (3, w, 0), "ontladen": (4, self.max_laden, w), "vasthouden": (7, self.max_laden, 0),
                    "zelfverbruik": (7, self.max_laden, self.max_ontladen)}[actie]
        if self._nodig(opdracht):
            await self._schrijf(*opdracht)
            log.info("SolarEdge: %s %s", actie, opdracht)


async def _sunspec_model(mb: Modbus, ids, offset) -> int:
    """Zoekt in de SunSpec-modellenketen het eerste model uit 'ids' en geeft het adres van het veld op 'offset'."""
    r = await mb.lees(40000, 2)
    if r != [0x5375, 0x6E53]:
        raise ConnectionError("Geen SunSpec-apparaat op dit adres (verkeerde unit-id?).")
    adres = 40002
    for _ in range(40):
        mid, lengte = await mb.lees(adres, 2)
        if mid == 0xFFFF:
            break
        if mid in ids:
            return adres + 2 + offset
        adres += 2 + lengte
    raise ConnectionError(f"SunSpec-model {ids} niet gevonden.")


# ============================================================ SMA (Sunny Boy Storage / Tripower Smart Energy)
class SMA(AccuOmvormer):
    """Modbus TCP poort 502, unit 3. Lezen: 30775 AC-vermogen, 30845 lading, 31393 laden, 31395 ontladen (W).
    Sturen: 40236 (2424 standaard, 2289 accu laden) met grenzen 40793/40795/40797/40799 en netsetpoint 40801.
    Vereist: Modbus aan en 'Prognosegestuurd laden' in Sunny Portal uit."""
    acties = {"laden", "vasthouden", "zelfverbruik"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.mb = Modbus(self.o["host"], int(self.o.get("modbus_poort", 502)), int(self.o.get("modbus_unit", 3)))
        a = cfg.get("accu", {})
        self.maxl, self.maxo = int(a.get("max_laden_kw", 5) * 1000), int(a.get("max_ontladen_kw", 5) * 1000)

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 502), "modbus_unit": extra.get("modbus_unit", 3)}, "accu": {}})

    async def start(self):
        await self.mb.open()

    async def stop(self):
        if self.sturen_aan and self._laatste:
            try:
                await self._schrijf(2424, 0, self.maxl, 0, self.maxo)
            except Exception as e:
                log.warning("SMA terugzetten mislukt: %s", e)
        self.mb.sluit()

    async def lees(self) -> dict:
        ac = s32(await self.mb.lees(30775, 2))
        soc = u32(await self.mb.lees(30845, 2))
        laden = u32(await self.mb.lees(31393, 2))
        ontladen = u32(await self.mb.lees(31395, 2))
        nan = lambda v: 0 if v in (0xFFFFFFFF, 0x80000000) else v
        ac, laden, ontladen = (0 if ac == -(1 << 31) else ac), nan(laden), nan(ontladen)
        return {"zon_kw": max(ac + laden - ontladen, 0) / 1000, "accu_kw": (laden - ontladen) / 1000, "accu_soc": float(nan(soc))}

    async def _schrijf(self, mod, chamin, chamax, dschmin, dschmax):
        await self.mb.schrijf(40236, naar_u32(mod))
        for adres, w in ((40793, chamin), (40795, chamax), (40797, dschmin), (40799, dschmax), (40801, 0)):
            await self.mb.schrijf(adres, naar_u32(w))

    async def _stuur(self, actie, kw, doel):
        w = min(int(kw * 1000), self.maxl)
        opdracht = {"laden": (2289, w, w, 0, 0), "vasthouden": (2424, 0, self.maxl, 0, 0),
                    "zelfverbruik": (2424, 0, self.maxl, 0, self.maxo)}[actie]
        if self._nodig(opdracht):
            await self._schrijf(*opdracht)
            log.info("SMA: %s", actie)


# ============================================================ Fronius Gen24 (lezen via Solar API, sturen via SunSpec 124)
class FroniusGen24(AccuOmvormer):
    """Lezen via de lokale Solar API. Sturen via Modbus TCP (unit 1), SunSpec-model 124:
    StorCtl_Mod (3), OutWRte (10), InWRte (11), InOutWRte_RvrtTms (13), ChaGriSet (15), InOutWRte_SF (23).
    Vereist in de Fronius-webinterface: Modbus aan en 'Sturing via Modbus' toegestaan, en laden uit het net toegestaan.
    De terugvaltijd (RvrtTms) zorgt dat de omvormer zelf teruggaat naar normaal als het brein stopt."""
    acties = {"laden", "vasthouden", "zelfverbruik"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.mb = Modbus(self.o["host"], int(self.o.get("modbus_poort", 502)), 1)
        self.basis = None
        self.sf = -2
        self.w_max = 5000.0
        self.http = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip}, "accu": {}})

    async def start(self):
        from .echt import Fronius
        self.http = Fronius(self.o["host"])
        await self.http.start()
        if self.sturen_aan:
            veld0 = await _sunspec_model(self.mb, (124,), 0)
            self.basis = veld0
            regs = await self.mb.lees(self.basis, 24)
            self.sf = s16(regs[23])
            self.w_max = regs[0] * 10 ** s16(regs[16]) or 5000.0

    async def stop(self):
        if self.sturen_aan and self._laatste and self.basis:
            try:
                await self._schrijf(0, 100)
            except Exception as e:
                log.warning("Fronius terugzetten mislukt: %s", e)
        self.mb.sluit()
        if self.http:
            await self.http.stop()

    async def lees(self) -> dict:
        return await self.http.lees()

    async def _schrijf(self, mod, out_pct, grid=None):
        raw = lambda pct: int(round(pct / 10 ** self.sf)) & 0xFFFF
        await self.mb.schrijf(self.basis + 13, [600])           # na 10 minuten zonder bevestiging: terug naar normaal
        if grid is not None:
            await self.mb.schrijf(self.basis + 15, [grid])
        await self.mb.schrijf(self.basis + 3, [mod])
        await self.mb.schrijf(self.basis + 10, [raw(out_pct)])

    async def _stuur(self, actie, kw, doel):
        pct = min(100.0, kw * 1000 / self.w_max * 100)
        opdracht = {"laden": (2, -round(pct), 1), "vasthouden": (2, 0, None), "zelfverbruik": (0, 100, None)}[actie]
        if self._nodig(opdracht):
            await self._schrijf(*opdracht)
            log.info("Fronius: %s %s", actie, opdracht)


# ============================================================ Sessy
class Sessy(AccuOmvormer):
    """Lokale API via de bibliotheek 'sessypy' (ook gebruikt door Home Assistant). Inloggen met gebruikersnaam en
    wachtwoord van de sticker. Sturen: strategie 'API' met setpoint in watt (− = laden, + = leveren); terug: 'NOM'."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.dev = None
        self._strategie_origineel = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "gebruiker": extra.get("gebruiker", ""), "wachtwoord": extra.get("wachtwoord", "")}, "accu": {}})

    async def start(self):
        from sessypy.devices import get_sessy_device
        self.dev = await get_sessy_device(self.o["host"], self.o.get("gebruiker", ""), self.o.get("wachtwoord", ""))
        try:
            self._strategie_origineel = (await self.dev.get_power_strategy()).get("strategy")
        except Exception:
            pass

    async def stop(self):
        if self.dev and self.sturen_aan and self._laatste:
            from sessypy.const import SessyPowerStrategy as S
            try:
                await self.dev.set_power_strategy(self._strategie_origineel or S.NOM)
            except Exception as e:
                log.warning("Sessy terugzetten mislukt: %s", e)

    async def lees(self) -> dict:
        if self.dev is None:
            await self.start()
        s = (await self.dev.get_power_status()).get("sessy", {})
        return {"accu_soc": float(s.get("state_of_charge", 0)) * 100, "accu_kw": -float(s.get("power", 0)) / 1000}

    async def _stuur(self, actie, kw, doel):
        from sessypy.const import SessyPowerStrategy as S
        w = int(kw * 1000)
        opdracht = {"laden": ("api", -w), "ontladen": ("api", w), "vasthouden": ("api", 0), "zelfverbruik": ("nom", 0)}[actie]
        if not self._nodig(opdracht):
            return
        if opdracht[0] == "nom":
            await self.dev.set_power_strategy(S.NOM)
        else:
            await self.dev.set_power_strategy(S.API)
            await self.dev.set_power_setpoint(opdracht[1])
        log.info("Sessy: %s", opdracht)


# ============================================================ alleen uitlezen
class Enphase:
    """IQ Gateway (Envoy) via de bibliotheek 'pyenphase' (Home Assistant). Vanaf firmware 7 is een token nodig:
    log in met je Enlighten-account. Alleen uitlezen; IQ Batteries sturen kan alleen via Enphase."""
    kan_sturen = False

    def __init__(self, cfg: dict):
        self.o = cfg["omvormer"]
        self.env = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "gebruiker": extra.get("gebruiker"), "wachtwoord": extra.get("wachtwoord")}})

    async def start(self):
        from pyenphase import Envoy
        self.env = Envoy(self.o["host"])
        await self.env.setup()
        await self.env.authenticate(username=self.o.get("gebruiker") or None, password=self.o.get("wachtwoord") or None)

    async def stop(self):
        return

    async def lees(self) -> dict:
        if self.env is None:
            await self.start()
        d = await self.env.update()
        uit = {"zon_kw": (d.system_production.watts_now if d.system_production else 0) / 1000}
        if d.system_net_consumption:
            uit["net_kw"] = d.system_net_consumption.watts_now / 1000
        if d.encharge_aggregate:
            uit["accu_soc"] = float(d.encharge_aggregate.state_of_charge)
        return uit

    async def accu(self, *a):
        raise NietToegestaan("Enphase-accu's kunnen alleen via Enphase zelf gestuurd worden.")

    async def exportlimiet(self, kw):
        if kw == 0:
            raise NietToegestaan("Aftoppen is voor Enphase niet gekoppeld.")


class DeyeSolarman:
    """Deye 3-fase hybride (SUN-…-SG0xLP3 en gelijken) via de Solarman-wifistick, bibliotheek 'pysolarmanv5'.
    Het serienummer van de stick staat op de sticker. Registers: 588 lading, 590 accu (+ = leveren), 625 net, 672/673 zon.
    Alleen uitlezen; Growatt, Solis en Sofar hebben andere registerkaarten en volgen nog."""
    kan_sturen = False

    def __init__(self, cfg: dict):
        self.o = cfg["omvormer"]
        self.c = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "serienummer": extra.get("serienummer")}})

    async def start(self):
        from pysolarmanv5 import PySolarmanV5Async
        if not self.o.get("serienummer"):
            raise ConnectionError("Vul het serienummer van de Solarman-stick in (staat op de sticker).")
        self.c = PySolarmanV5Async(self.o["host"], int(self.o["serienummer"]), port=8899, mb_slave_id=1)
        await self.c.connect()

    async def stop(self):
        if self.c:
            await self.c.disconnect()

    async def lees(self) -> dict:
        if self.c is None:
            await self.start()
        soc = (await self.c.read_holding_registers(588, 1))[0]
        accu = s16((await self.c.read_holding_registers(590, 1))[0])
        net = s16((await self.c.read_holding_registers(625, 1))[0])
        pv1, pv2 = await self.c.read_holding_registers(672, 2)
        if soc > 100:
            raise ValueError("Onverwachte waarden: dit Deye-model gebruikt een andere registerkaart.")
        return {"zon_kw": (pv1 + pv2) / 1000, "accu_kw": -accu / 1000, "accu_soc": float(soc), "net_kw": net / 1000}

    async def accu(self, *a):
        raise NietToegestaan("Sturen van Deye volgt; nu alleen uitlezen.")

    async def exportlimiet(self, kw):
        if kw == 0:
            raise NietToegestaan("Aftoppen is voor Deye niet gekoppeld.")


class Solis:
    """Solis hybride omvormers (S6-EH3P…-H, S5-EH1P…, RHI) via Modbus TCP op de LAN-stick of wifi-stick, poort 502, unit 1.
    Alleen uitlezen. Registers (input, functiecode 4) volgens het Solis RS485-Modbus-protocol voor hybrides:
    33057-33058 PV-vermogen (u32, W), 33130-33131 meter (s32, W, + = teruglevering), 33135 accu-richting (0 laden, 1 ontladen),
    33139 accu-lading (%), 33147 huisverbruik (u16, W), 33149-33150 accuvermogen (s32, W). 32-bits waarden: eerste woord hoogste.
    Het teken van het accuvermogen is niet bij alle firmware gelijk; daarom bepaalt register 33135 de richting."""
    kan_sturen = False

    def __init__(self, cfg: dict):
        o = cfg["omvormer"]
        self.mb = Modbus(o["host"], int(o.get("modbus_poort", 502)), int(o.get("modbus_unit", 1)))

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"omvormer": {"host": ip, "modbus_poort": extra.get("modbus_poort", 502), "modbus_unit": extra.get("modbus_unit", 1)}})

    async def start(self):
        await self.mb.open()

    async def stop(self):
        self.mb.sluit()

    async def lees(self) -> dict:
        try:
            r = await self.mb.lees(33130, 21, "input")        # 33130 … 33150 in één keer
        except IOError:                                       # sommige sticks weigeren een groot blok: dan per register
            r = [0] * 21
            for i in (0, 1, 5, 9, 17, 19, 20):
                r[i] = (await self.mb.lees(33130 + i, 1, "input"))[0]
        zon = u32(await self.mb.lees(33057, 2, "input"))
        meter = s32(r[0:2])
        richting, soc, last, accu = r[5], r[9], r[17], s32(r[19:21])
        if soc > 100 or richting > 1:
            raise ValueError("Onverwachte waarden: dit Solis-model gebruikt een andere registerkaart of de stick geeft geen antwoord van de omvormer.")
        accu_w = abs(accu) * (1 if richting == 0 else -1)      # + = laden
        net = -meter if meter else last + accu_w - zon         # + = afname; zonder meter volgt het uit de balans
        return {"zon_kw": max(zon, 0) / 1000, "accu_kw": accu_w / 1000, "accu_soc": float(soc), "net_kw": net / 1000}

    async def accu(self, *a):
        raise NietToegestaan("Sturen van Solis volgt; nu alleen uitlezen.")

    async def exportlimiet(self, kw):
        if kw == 0:
            raise NietToegestaan("Aftoppen is voor Solis niet gekoppeld.")


# ============================================================ meters
class ESPHomeP1:
    """SlimmeLezer of een andere ESPHome-P1-lezer via de native ESPHome-API (bibliotheek 'aioesphomeapi').
    Zoekt sensoren 'power_delivered/consumed' en 'power_returned/produced' (kW of W)."""

    def __init__(self, cfg_of_host, sleutel: str | None = None):
        if isinstance(cfg_of_host, dict):
            p = cfg_of_host["net"]["p1"]
            self.host, self.sleutel = p["host"], p.get("sleutel") or None
        else:
            self.host, self.sleutel = cfg_of_host, sleutel
        self.client = None
        self.ids: dict[int, tuple[str, float]] = {}
        self.waarden: dict[str, float] = {}

    @classmethod
    def voor_test(cls, ip, extra):
        return cls(ip, extra.get("sleutel"))

    async def start(self):
        from aioesphomeapi import APIClient
        self.client = APIClient(self.host, 6053, None, noise_psk=self.sleutel)
        await self.client.connect(login=True)
        entiteiten, _ = await self.client.list_entities_services()
        for e in entiteiten:
            oid = (getattr(e, "object_id", "") or "").lower()
            eenheid = (getattr(e, "unit_of_measurement", "") or "").lower()
            factor = 1.0 if eenheid == "kw" else 0.001
            if any(w in oid for w in ("power_delivered", "power_consumed")) and "l1" not in oid and "l2" not in oid and "l3" not in oid:
                self.ids[e.key] = ("af", factor)
            elif any(w in oid for w in ("power_returned", "power_produced")) and "l1" not in oid and "l2" not in oid and "l3" not in oid:
                self.ids[e.key] = ("terug", factor)
        if not self.ids:
            raise ConnectionError("Geen vermogenssensoren gevonden op deze ESPHome-lezer.")

        def nieuw(s):
            if s.key in self.ids and getattr(s, "state", None) is not None:
                naam, f = self.ids[s.key]
                self.waarden[naam] = float(s.state) * f
        self.client.subscribe_states(nieuw)

    async def stop(self):
        if self.client:
            await self.client.disconnect()

    async def lees(self) -> dict:
        if self.client is None:
            await self.start()
        for _ in range(20):
            if "af" in self.waarden:
                break
            await asyncio.sleep(0.25)
        return {"net_kw": self.waarden.get("af", 0.0) - self.waarden.get("terug", 0.0)}


class TibberPulse:
    """Live meting van de Tibber Pulse via de officiële Tibber-API (websocket, liveMeasurement)."""

    def __init__(self, cfg: dict):
        self.token = cfg["prijzen"].get("tibber_token", "")
        self.net_kw = None
        self.taak = None
        self.fout = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"prijzen": {"tibber_token": extra.get("tibber_token", "")}})

    async def start(self):
        if not self.token:
            raise ConnectionError("Vul je Tibber-code in bij Contract.")
        self.taak = asyncio.create_task(self._luister())

    async def stop(self):
        if self.taak:
            self.taak.cancel()

    async def _luister(self):
        kop = {"Authorization": f"Bearer {self.token}"}
        while True:
            try:
                from ..net import sessie
                async with sessie(None, 15) as s:
                    async with s.post("https://api.tibber.com/v1-beta/gql", headers=kop,
                                      json={"query": "{ viewer { websocketSubscriptionUrl homes { id features { realTimeConsumptionEnabled } } } }"}) as r:
                        d = (await r.json())["data"]["viewer"]
                    home = next((h["id"] for h in d["homes"] if (h.get("features") or {}).get("realTimeConsumptionEnabled")), None)
                    if not home:
                        self.fout = "Geen Tibber Pulse gevonden op je account."
                        return
                    async with s.ws_connect(d["websocketSubscriptionUrl"], protocols=["graphql-transport-ws"], headers={"User-Agent": "Energiebrein/1.0"}) as ws:
                        await ws.send_json({"type": "connection_init", "payload": {"token": self.token}})
                        await ws.receive_json()
                        await ws.send_json({"id": "1", "type": "subscribe", "payload": {"query": f'subscription {{ liveMeasurement(homeId: "{home}") {{ power powerProduction }} }}'}})
                        async for msg in ws:
                            data = json.loads(msg.data)
                            if data.get("type") == "next":
                                m = data["payload"]["data"]["liveMeasurement"]
                                self.net_kw = ((m.get("power") or 0) - (m.get("powerProduction") or 0)) / 1000
                                self.fout = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.fout = f"Tibber Pulse: {e}"
                await asyncio.sleep(30)

    async def lees(self) -> dict:
        for _ in range(40):
            if self.net_kw is not None:
                return {"net_kw": self.net_kw}
            if self.fout:
                break
            await asyncio.sleep(0.25)
        raise ConnectionError(self.fout or "Nog geen meting van de Tibber Pulse.")


class TeslaWallConnector:
    """Tesla Wall Connector Gen 3: lokaal uitlezen via /api/1/vitals. Sturen kan Tesla niet lokaal; laden plannen
    loopt dan via de auto of de Tesla-app."""
    kan_sturen = False

    def __init__(self, cfg_of_host):
        self.host = cfg_of_host if isinstance(cfg_of_host, str) else cfg_of_host["laadpaal"]["host"]
        self.sessie = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls(ip)

    async def start(self):
        self.sessie = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def stop(self):
        if self.sessie:
            await self.sessie.close()

    async def lees(self) -> dict:
        async with self.sessie.get(f"http://{self.host}/api/1/vitals") as r:
            d = await r.json(content_type=None)
        stroom = sum(float(d.get(k) or 0) for k in ("currentA_a", "currentB_a", "currentC_a"))
        spanning = float(d.get("voltageA_v") or 230)
        return {"auto_kw": stroom * spanning / 1000, "auto_verbonden": bool(d.get("vehicle_connected")),
                "auto_geladen_kwh": float(d.get("session_energy_wh") or 0) / 1000}

    async def laadstroom(self, ampere):
        raise NietToegestaan("De Tesla Wall Connector kan niet lokaal gestuurd worden.")


# ======================= Easee (via de Easee-cloud) =======================
class Easee403(RuntimeError):
    pass


class Easee:
    """Easee-laders hebben geen lokale instellingen of API; uitlezen en sturen gaat via je Easee-account (api.easee.com),
    net als bij de Home Assistant- en openHAB-koppelingen. We vragen de status hooguit elke 30 seconden op en
    veranderen de laadstroom alleen als die echt anders moet (dynamicChargerCurrent), om de cloud niet te overbelasten."""
    kan_sturen = True
    API = "https://api.easee.com/api"
    # chargerOpMode: 1 losgekoppeld, 2 wacht op start, 3 laadt, 4 klaar, 5 fout, 6 klaar om te laden, 7 wacht op autorisatie
    VERBONDEN = {2, 3, 4, 6, 7}

    def __init__(self, cfg: dict):
        c = cfg.get("laadpaal", {})
        self.gebruiker = (c.get("gebruiker") or "").strip()
        self.wachtwoord = c.get("wachtwoord") or ""
        self.lader_id = (c.get("serienummer") or "").strip().upper()
        self.sessie = None
        self.token = self.ververs_token = None
        self.token_tot = None
        self._cache = (None, None)
        self._laatste_a = None
        self._laatste_a_tijd = None
        self._route = "lader"
        self._site_id = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"laadpaal": {"gebruiker": extra.get("gebruiker", ""), "wachtwoord": extra.get("wachtwoord", ""),
                                 "serienummer": extra.get("serienummer", "")}})

    async def start(self):
        from ..net import sessie
        if not self.gebruiker or not self.wachtwoord:
            raise RuntimeError("Vul het e-mailadres (of telefoonnummer) en wachtwoord van je Easee-account in.")
        from ..net import uitleg
        self.sessie = sessie(20)
        try:
            await self._login()
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"de Easee-cloud is niet bereikbaar ({uitleg(e)}). Heeft dit apparaat internet?")
        try:
            lijst = await self._vraag("GET", "/chargers") or []
        except RuntimeError:
            lijst = None
        ids = [str(x.get("id", "")).upper() for x in (lijst or [])]
        if lijst is not None and not ids:
            raise RuntimeError("inloggen bij Easee lukt, maar er staat geen enkele lader op dit account. De lader is dan aan een ander "
                               "account gekoppeld (vaak van de installateur of van wie hem eerst registreerde). Vraag die persoon je in de "
                               "Easee-app toe te voegen (lader → Instellingen → Toegang), of laat Easee-support de lader op jouw account zetten.")
        if not self.lader_id:
            self.lader_id = ids[0]
        elif ids and self.lader_id not in ids:
            raise RuntimeError(f"op dit Easee-account staat niet lader {self.lader_id}, maar {', '.join(ids)}. "
                               "Controleer het serienummer (8 tekens, staat in de Easee-app en op de sticker) of laat het veld leeg.")
        log.info("Easee gekoppeld: lader %s", self.lader_id)

    async def stop(self):
        if self.sessie:
            await self.sessie.close()

    async def _login(self):
        async with self.sessie.post(f"{self.API}/accounts/login", json={"userName": self.gebruiker, "password": self.wachtwoord}) as r:
            if r.status in (400, 401, 403):
                raise RuntimeError("Easee accepteert dit e-mailadres of wachtwoord niet.")
            r.raise_for_status()
            d = await r.json(content_type=None)
        self._zet_token(d)

    def _zet_token(self, d):
        self.token, self.ververs_token = d["accessToken"], d.get("refreshToken")
        self.token_tot = datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expiresIn") or 3600) - 120)

    async def _vraag(self, methode, pad, json=None):
        if not self.token or datetime.now(timezone.utc) >= self.token_tot:
            try:
                async with self.sessie.post(f"{self.API}/accounts/refresh_token",
                                            json={"accessToken": self.token, "refreshToken": self.ververs_token}) as r:
                    r.raise_for_status()
                    self._zet_token(await r.json(content_type=None))
            except Exception:
                await self._login()
        kop = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
               "Content-Type": "application/json;charset=UTF-8", "User-Agent": "Energiebrein/1.1 (+https://accumaatje.nl)"}
        async with self.sessie.request(methode, self.API + pad, json=json, headers=kop) as r:
            if r.status == 401:
                self.token = None
            if r.status == 429:
                raise RuntimeError("Easee vraagt even rustiger aan te doen (te veel verzoeken); het brein probeert het zo opnieuw.")
            if r.status == 403:
                antw = (await r.text())[:160].strip().replace("\n", " ")
                log.warning("Easee 403 op %s %s: %s", methode, pad, antw)
                raise Easee403(f"Easee weigert {methode} {pad} (403){': ' + antw if antw else ''}. Je bent wel ingelogd. "
                               "Ben je in het Easee-portaal eigenaar van de lader, stuur deze melding dan door.")
            r.raise_for_status()
            tekst = await r.text()
            return _json.loads(tekst) if tekst.strip() else {}

    async def lees(self) -> dict:
        nu = datetime.now(timezone.utc)
        tijd, d = self._cache
        if not d or (nu - tijd).total_seconds() >= 30:
            d = await self._status()
            self._cache = (nu, d)
        modus = int(d.get("chargerOpMode") or 0)
        return {"auto_kw": float(d.get("totalPower") or 0), "auto_verbonden": modus in self.VERBONDEN,
                "auto_geladen_kwh": float(d.get("sessionEnergy") or 0)}

    async def _status(self) -> dict:
        """Easee blokkeert de losse statusroute steeds vaker (403). De status van de locatie werkt wel; die gebruikt
        de Home Assistant-koppeling (pyeasee) ook. We onthouden welke route werkt."""
        if self._route == "lader":
            try:
                return await self._vraag("GET", f"/chargers/{self.lader_id}/state")
            except Easee403:
                self._route = "locatie"
        if self._site_id is None:
            for site in await self._vraag("GET", "/sites") or []:
                st = await self._vraag("GET", f"/sites/{site['id']}/state")
                if self._uit_locatie(st) is not None:
                    self._site_id = site["id"]
                    break
            else:
                raise RuntimeError(f"Easee: lader {self.lader_id} staat op geen enkele locatie van dit account.")
        d = self._uit_locatie(await self._vraag("GET", f"/sites/{self._site_id}/state"))
        if d is None:
            self._site_id = None
            raise RuntimeError(f"Easee: lader {self.lader_id} niet gevonden in de status van de locatie.")
        return d

    def _uit_locatie(self, st: dict) -> dict | None:
        for kring in (st or {}).get("circuitStates") or []:
            for lader in kring.get("chargerStates") or []:
                if str(lader.get("chargerID", "")).upper() == self.lader_id:
                    return lader.get("chargerState") or {}
        return None

    async def laadstroom(self, ampere):
        a = int(max(0, min(32, round(ampere))))
        nu = datetime.now(timezone.utc)
        if a == self._laatste_a and self._laatste_a_tijd and (nu - self._laatste_a_tijd).total_seconds() < 600:
            return
        # niet vaker dan eens per minuut wijzigen, behalve naar 0 (stoppen moet direct kunnen)
        if a != 0 and self._laatste_a_tijd and (nu - self._laatste_a_tijd).total_seconds() < 60:
            return
        try:  # de opdracht die de Easee-app zelf ook gebruikt
            await self._vraag("POST", f"/chargers/{self.lader_id}/commands/set_dynamic_charger_current", json={"amps": a, "minutes": 0})
        except (Easee403, aiohttp.ClientResponseError):
            await self._vraag("POST", f"/chargers/{self.lader_id}/settings", json={"dynamicChargerCurrent": a})
        self._laatste_a, self._laatste_a_tijd = a, nu
        log.info("Easee: laadstroom %s A", a)

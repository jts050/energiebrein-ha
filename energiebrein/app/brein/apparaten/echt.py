"""Drivers voor echte apparaten. Alles lokaal, zonder cloud van fabrikant of leverancier.

Elke driver stuurt een opdracht alleen als die echt verandert: omvormers schrijven instellingen
naar intern geheugen, en dat moet je niet elke 10 seconden doen."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("apparaten")


# ======================= GoodWe (omvormer + accu) =======================
async def _poort_open(host: str, poort: int, wacht: float = 2.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, poort), timeout=wacht)
        w.close()
        return True
    except Exception:
        return False


async def goodwe_verbind(host: str, familie: str | None = None):
    """GoodWe kan op twee manieren lokaal praten: UDP poort 8899 (wifi-module) of Modbus TCP poort 502 (LAN-module en
    nieuwere modules). We proberen beide, met een ruimere wachttijd dan standaard (via een wifi-versterker duurt het
    antwoord soms langer dan 1 seconde). Lukt niets, dan een melding in gewone taal met wat je kunt controleren."""
    import goodwe
    pogingen, fouten = [], []
    if await _poort_open(host, 502):
        pogingen.append(502)
    pogingen += [8899] + ([] if 502 in pogingen else [502])
    for poort in pogingen:
        try:
            return await asyncio.wait_for(goodwe.connect(host, port=poort, family=familie or None, timeout=3, retries=3), timeout=60)
        except Exception as e:
            fouten.append(f"poort {poort}: {type(e).__name__}")
            log.warning("GoodWe via poort %s lukt niet: %s", poort, e)
    raise GoodWeNietBereikbaar(host, fouten)


def _eigen_ip_tekst() -> str:
    try:
        from ..zoeken import eigen_ip
        ip = eigen_ip()
        return f" (nu: {ip})" if ip else ""
    except Exception:
        return ""


class GoodWeNietBereikbaar(RuntimeError):
    def __init__(self, host, fouten):
        self.host, self.fouten = host, fouten
        net = ".".join(host.split(".")[:3]) + "."
        super().__init__(
            f"De GoodWe op {host} geeft geen antwoord op de lokale verbinding. Controleer: "
            "1) Windows-firewall: sta Python toe op privénetwerken (Windows-beveiliging → Firewall en netwerkbeveiliging → "
            "Een app toestaan via de firewall → python.exe, vinkje bij Privé). "
            f"2) Zit deze computer op hetzelfde netwerk als de omvormer? Het IP-adres van deze computer moet ook met {net} beginnen{_eigen_ip_tekst()}. "
            "3) Sommige nieuwere GoodWe-wifimodules staan lokaal uitlezen niet toe; dan werkt het alleen via een LAN-module met netwerkkabel. "
            f"(Geprobeerd: {', '.join(fouten)})")


EIGEN_VERBRUIK = {"GENERAL", "SELF_USE"}


class GoodWe:
    """Via de open-source 'goodwe'-bibliotheek (dezelfde als de Home Assistant-integratie), lokaal via UDP/Modbus."""
    kan_sturen = True
    # vasthouden: via EMS-modus BATTERY_STANDBY (nieuwere firmware). Lukt dat niet, dan eigen verbruik.

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg["omvormer"]["host"]
        self.inv = None
        self._laatste_opdracht = None
        self._laatste_export = "onbekend"
        self.stand: dict = {}
        self.afwijking: str | None = None
        self._standby_kan: bool | None = None     # None = nog niet geprobeerd
        self._standby_actief = False
        self.meter_kw: float | None = None
        self.bms: dict = {}

    async def start(self):
        self.inv = await goodwe_verbind(self.host, self.cfg["omvormer"].get("goodwe_familie"))
        log.info("GoodWe verbonden: %s %s", self.inv.model_name, self.inv.serial_number)
        # Stond de accu nog in stand-by van een vorige keer (bijv. pc uitgevallen)? Dan eerst terug naar automatisch.
        try:
            from goodwe import EMSMode
            if await asyncio.wait_for(self.inv.get_ems_mode(), timeout=10) == EMSMode.BATTERY_STANDBY:
                log.warning("GoodWe stond nog in stand-by van een vorige keer; terug naar automatisch")
                self._standby_actief = True
                await self._standby(False)
        except Exception:
            pass

    async def stop(self):
        """Bij afsluiten terug naar de gewone stand. Anders blijft een laad- of ontlaadschema in de omvormer staan
        terwijl er niemand meer op let."""
        if self.inv is not None and self._standby_actief:
            await self._standby(False)
        if self.inv is not None and self._laatste_opdracht and self._laatste_opdracht[0].name not in EIGEN_VERBRUIK:
            try:
                await asyncio.wait_for(self.inv.set_operation_mode(self._eigen_verbruik()), timeout=20)
                log.info("GoodWe teruggezet naar de gewone stand (zelfverbruik)")
            except Exception as e:
                log.warning("GoodWe terugzetten bij afsluiten mislukt: %s", e)

    async def _zeker(self):
        if self.inv is None:   # bij opstarten niet bereikbaar: opnieuw proberen
            try:
                await self.start()
            except Exception as e:
                raise RuntimeError(f"omvormer op {self.host} niet bereikbaar ({e})")

    async def lees(self) -> dict:
        await self._zeker()
        try:
            d = await self.inv.read_runtime_data()
        except Exception:
            # wifi-modules laten af en toe een vraag onbeantwoord: even wachten en nog één keer
            await asyncio.sleep(1.5)
            try:
                d = await self.inv.read_runtime_data()
            except Exception:
                self._missers = getattr(self, "_missers", 0) + 1
                if self._missers >= 6:          # een minuut niets: verbinding opnieuw opbouwen
                    log.warning("GoodWe reageert al %d keer niet, verbinding wordt opnieuw opgebouwd", self._missers)
                    self.inv, self._missers = None, 0
                raise
        self._missers = 0
        meter = d.get("meter_active_power_total")
        if meter is None:
            meter = d.get("active_power")
        self.meter_kw = None if meter is None else -float(meter) / 1000     # + = afname van het net, zoals de GoodWe-meter het ziet
        f = [d.get(f"meter_active_power{i}") for i in (1, 2, 3)]
        self.meter_fasen = [-float(v) / 1000 for v in f] if all(v is not None for v in f) else None
        fout = d.get("battery_error") or d.get("battery_warning")
        _a = lambda v: None if v is None else abs(v - 65536 if isinstance(v, int) and v > 32767 else v)
        self.bms = {"ontladen_a": _a(d.get("battery_discharge_limit")), "laden_a": d.get("battery_charge_limit"),
                    "spanning": d.get("vbattery1"),
                    "temperatuur": d.get("battery_temperature"), "stand": d.get("battery_mode_label"),
                    "fout": fout if fout not in (0, "0", "") else None}
        await self._controleer_stand()
        zon = (d.get("ppv") or d.get("ppv_total") or 0) / 1000
        accu_ontladen = (d.get("pbattery1") or 0) / 1000          # GoodWe: + = ontladen
        export = (d.get("active_power") or 0) / 1000              # GoodWe: + = teruglevering
        return {"zon_kw": zon, "accu_kw": -accu_ontladen, "accu_soc": float(d.get("battery_soc") or 0),
                "net_kw": -export}

    @property
    def kan_vasthouden(self) -> bool:
        return self._standby_kan is not False

    async def _standby(self, aan: bool) -> bool:
        """Accu in stand-by (niet laden, niet leveren) of terug naar automatisch. Geeft True als het gelukt is."""
        from goodwe import EMSMode
        doel = EMSMode.BATTERY_STANDBY if aan else EMSMode.AUTO
        try:
            await asyncio.wait_for(self.inv.set_ems_mode(doel), timeout=15)
            nu = await asyncio.wait_for(self.inv.get_ems_mode(), timeout=15)
        except Exception as e:
            log.warning("GoodWe EMS-modus %s lukt niet: %s", doel.name, e)
            return False
        if nu != doel:
            log.warning("GoodWe EMS-modus is %s na opdracht %s", nu, doel.name)
            return False
        self._standby_actief = aan
        log.info("GoodWe: accu %s", "in stand-by (lading vasthouden)" if aan else "weer automatisch")
        return True

    async def lees_stand(self) -> dict:
        """Welke stand staat er écht in de omvormer: werkmodus en (bij nieuwere firmware) EMS-modus."""
        stand = {}
        try:
            m = await self.inv.get_operation_mode()
            stand["werkmodus"] = m.name if m is not None else "onbekend"
        except Exception as e:
            stand["werkmodus"] = f"niet uit te lezen ({type(e).__name__})"
        try:
            e = await self.inv.get_ems_mode()
            if e is not None:
                stand["ems"] = e.name
        except Exception:
            pass
        try:
            dod = int(await self.inv.get_ongrid_battery_dod())
            if 0 <= dod <= 100:
                stand["ondergrens_pct"] = 100 - dod        # de accu ontlaadt (op het net) niet verder dan dit
        except Exception:
            pass
        return stand

    async def _controleer_stand(self):
        """Elke 2 minuten nakijken of de omvormer doet wat Energiebrein heeft gevraagd."""
        nu = datetime.now(timezone.utc)
        if getattr(self, "_stand_tijd", None) and (nu - self._stand_tijd).total_seconds() < 120:
            return
        self._stand_tijd = nu
        try:
            nieuw = await asyncio.wait_for(self.lees_stand(), timeout=20)
        except Exception:
            return
        # lukt het uitlezen van de ondergrens een keer niet, dan de laatst bekende houden (anders springt het plan heen en weer)
        if "ondergrens_pct" not in nieuw and "ondergrens_pct" in self.stand:
            nieuw["ondergrens_pct"] = self.stand["ondergrens_pct"]
        self.stand = nieuw
        verwacht = self._laatste_opdracht[0].name if self._laatste_opdracht else None
        wm, ems = self.stand.get("werkmodus"), self.stand.get("ems")
        self.afwijking = None
        if ems and ems not in ("AUTO",) and not (ems == "BATTERY_STANDBY" and self._standby_actief):
            self.afwijking = (f"De omvormer staat in EMS-modus {ems}. Die gaat vóór de werkmodus en wordt niet door Energiebrein ingesteld; "
                              "waarschijnlijk stuurt een andere partij (bijvoorbeeld je energieleverancier via de GoodWe-cloud, of een instelling "
                              "in de SolarGo-app) de accu aan.")
        elif verwacht and wm and wm != verwacht and not wm.startswith("niet") and not (verwacht in EIGEN_VERBRUIK and wm in EIGEN_VERBRUIK):
            wm = {"ECO": "TOU-modus (tijdschema)", "ECO_CHARGE": "TOU-modus met laadschema", "ECO_DISCHARGE": "TOU-modus met ontlaadschema"}.get(wm, wm)
            verwacht = "eigen verbruik" if verwacht in EIGEN_VERBRUIK else verwacht
            self.afwijking = (f"Energiebrein vroeg {verwacht}, maar de omvormer staat op {wm}. De opdracht is niet aangekomen of wordt "
                              "overschreven door een andere partij (energieleverancier, GoodWe-cloud of SolarGo-app).")
            self._laatste_opdracht = None      # volgende ronde opnieuw sturen
        if self.afwijking:
            log.warning("GoodWe: %s (stand: %s)", self.afwijking, self.stand)

    async def zet_ondergrens(self, pct: int) -> int:
        """Ontlaaddiepte (DoD op het net) aanpassen, alleen na een klik van de gebruiker. Nooit onder 10%:
        de meeste lithium-accu's hebben dat als veilige reserve."""
        await self._zeker()
        pct = int(max(10, min(95, pct)))
        await asyncio.wait_for(self.inv.set_ongrid_battery_dod(100 - pct), timeout=20)
        self._stand_tijd = None
        self.stand = await asyncio.wait_for(self.lees_stand(), timeout=20)
        nieuw = self.stand.get("ondergrens_pct")
        log.info("GoodWe: ondergrens gezet op %s%% (omvormer meldt nu %s%%)", pct, nieuw)
        if nieuw is not None and abs(nieuw - pct) > 1:
            raise RuntimeError(f"de omvormer heeft de nieuwe ondergrens niet overgenomen (hij meldt nog {nieuw}%). "
                               "Pas hem aan in de SolarGo-app.")
        return pct

    async def accu(self, actie: str, kw: float = 0.0, doel_soc: float = 100.0):
        from goodwe import OperationMode
        await self._zeker()
        a = self.cfg["accu"]
        if actie == "vasthouden" and self._standby_kan is not False:
            if self._standby_actief:
                return
            if self._laatste_opdracht is None or self._laatste_opdracht[0].name not in EIGEN_VERBRUIK:
                await self.inv.set_operation_mode(self._eigen_verbruik())     # stand-by werkt vanuit eigen verbruik
                self._laatste_opdracht = (self._eigen_verbruik(), 100, 100)
            if await self._standby(True):
                self._standby_kan = True
                return
            self._standby_kan = False                                         # niet ondersteund: voortaan eigen verbruik
            log.warning("GoodWe: lading vasthouden wordt niet ondersteund, ik gebruik eigen verbruik")
        if self._standby_actief:
            if not await self._standby(False):
                raise RuntimeError("de accu staat in stand-by en wil niet terug naar automatisch. Zet in SolarGo de EMS-modus op Auto.")
        # GoodWe verwacht het vermogen als % van het vermogen van de omvormer (niet van het ingestelde maximum)
        vol = float(self.cfg["omvormer"].get("vermogen_kw") or max(a["max_laden_kw"], a["max_ontladen_kw"]))
        if actie == "laden":
            pct = max(1, min(100, round(min(kw, a["max_laden_kw"]) / vol * 100)))
            opdracht = (OperationMode.ECO_CHARGE, pct, int(doel_soc))
        elif actie == "ontladen":
            pct = max(1, min(100, round(min(kw, a["max_ontladen_kw"]) / vol * 100)))
            opdracht = (OperationMode.ECO_DISCHARGE, pct, int(a["min_soc"]))
        else:
            # zelfverbruik, en ook 'vasthouden': vroeger 'laden met 1%', maar sommige GoodWe-modellen laden daarbij op vol
            # vermogen uit het net en dat schema blijft in de omvormer staan. Daarom gewoon eigen verbruik.
            opdracht = (self._eigen_verbruik(), 100, 100)
        if opdracht != self._laatste_opdracht:
            await self.inv.set_operation_mode(*opdracht)
            log.info("GoodWe: %s %s%% doel %s%%", opdracht[0].name, opdracht[1], opdracht[2])
            if opdracht[0].name in EIGEN_VERBRUIK:
                await self._controleer_eigen_verbruik()
            self._laatste_opdracht = opdracht

    def _eigen_verbruik(self):
        """Nieuwere GoodWe's (zoals in SolarGo 'Eigen verbruik') gebruiken werkmodus SELF_USE, oudere GENERAL."""
        from goodwe import OperationMode
        try:
            from goodwe.model import is_745_platform
            if is_745_platform(self.inv):
                return OperationMode.SELF_USE
        except Exception:
            pass
        return getattr(self, "_voorkeur_eigen", None) or OperationMode.GENERAL

    async def _controleer_eigen_verbruik(self):
        """Direct nakijken of de omvormer echt op eigen verbruik staat. Zo niet (bijv. nog in TOU-modus), de andere
        variant proberen; lukt dat ook niet, dan een duidelijke melding."""
        from goodwe import OperationMode
        try:
            nu = await asyncio.wait_for(self.inv.get_operation_mode(), timeout=15)
        except Exception:
            return                                  # uitlezen lukt niet: de regelmatige controle vangt het later op
        if nu is None or nu.name in EIGEN_VERBRUIK:
            return
        ander = OperationMode.SELF_USE if self._eigen_verbruik() == OperationMode.GENERAL else OperationMode.GENERAL
        log.warning("GoodWe staat na de opdracht nog op %s; ik probeer %s", nu.name, ander.name)
        try:
            await self.inv.set_operation_mode(ander)
            nu = await asyncio.wait_for(self.inv.get_operation_mode(), timeout=15)
        except Exception:
            nu = None
        if nu is not None and nu.name in EIGEN_VERBRUIK:
            self._voorkeur_eigen = ander
            return
        raise RuntimeError(f"de omvormer blijft in {'TOU-modus' if nu is None or nu.name.startswith('ECO') else nu.name} staan en gaat niet "
                           "naar eigen verbruik. Zet in de SolarGo-app bij Bedrijfsmodus 'Eigen verbruik' aan.")

    async def exportlimiet(self, kw: float | None):
        waarde = 0 if kw == 0 else None
        if waarde != self._laatste_export:
            await self._zeker()
            # None = terug naar het maximum van de omvormer
            await self.inv.set_grid_export_limit(0 if waarde == 0 else 10000)
            self._laatste_export = waarde
            log.info("GoodWe: exportlimiet %s", "0 W (aftoppen)" if waarde == 0 else "normaal")


# ======================= HomeWizard P1-meter =======================
class HomeWizardP1:
    """Lokale API van de HomeWizard P1-meter (aanzetten in de HomeWizard-app: Instellingen → Meters → Lokale API)."""

    def __init__(self, cfg: dict):
        self.url = f"http://{cfg['net']['p1']['host']}/api/v1/data"
        self.sessie: aiohttp.ClientSession | None = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls({"net": {"p1": {"host": ip}}})

    async def start(self):
        self.sessie = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def stop(self):
        if self.sessie:
            await self.sessie.close()

    async def lees(self) -> dict:
        async with self.sessie.get(self.url) as r:
            if r.status == 403:
                raise PermissionError("De lokale API van de HomeWizard staat uit. Zet hem aan in de HomeWizard-app: Instellingen → Meters → je meter → Lokale API.")
            d = await r.json(content_type=None)
        f = [d.get(f"active_power_l{i}_w") for i in (1, 2, 3)]
        self.fasen = [v / 1000 for v in f] if all(v is not None for v in f) else None     # + = afname, per fase
        return {"net_kw": d["active_power_w"] / 1000}


# ======================= Laadpaal via OCPP 1.6 =======================
class OCPPLaadpaal:
    """Het kastje is zelf de OCPP-server. Zet in de app van je laadpaal de backend/OCPP-URL op
    ws://<ip-van-dit-kastje>:<poort>/<naam>. Geen cloud van de laadpaalfabrikant nodig."""

    def __init__(self, cfg: dict):
        self.cfg = cfg["laadpaal"]
        self.cp = None
        self.server = None
        self.kw = 0.0
        self.verbonden = False
        self.sessie_start_wh: float | None = None
        self.meter_wh = 0.0
        self._laatste_a: float | None = None
        self._tx = 0

    async def start(self):
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
        from ocpp.routing import on
        from ocpp.v16 import ChargePoint, call_result
        from ocpp.v16.enums import Action, RegistrationStatus, AuthorizationStatus
        paal = self

        class Paal(ChargePoint):
            @on(Action.boot_notification)
            async def boot(self, **_):
                return call_result.BootNotification(current_time=datetime.now(timezone.utc).isoformat(),
                                                    interval=60, status=RegistrationStatus.accepted)

            @on(Action.heartbeat)
            async def hart(self, **_):
                return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

            @on(Action.authorize)
            async def auth(self, **_):
                return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

            @on(Action.status_notification)
            async def status(self, status, **_):
                paal.verbonden = status in ("Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing")
                if status == "Available":
                    paal.sessie_start_wh = None
                return call_result.StatusNotification()

            @on(Action.start_transaction)
            async def start_tx(self, meter_start, **_):
                paal._tx += 1
                paal.sessie_start_wh = float(meter_start)
                return call_result.StartTransaction(transaction_id=paal._tx, id_tag_info={"status": AuthorizationStatus.accepted})

            @on(Action.stop_transaction)
            async def stop_tx(self, **_):
                return call_result.StopTransaction()

            @on(Action.meter_values)
            async def meter(self, meter_value, **_):
                for mv in meter_value:
                    for s in mv.get("sampled_value", []):
                        m, v = s.get("measurand", "Energy.Active.Import.Register"), float(s["value"])
                        factor = 1000 if s.get("unit") in ("kW", "kWh") else 1
                        if m == "Power.Active.Import":
                            paal.kw = v * factor / 1000
                        elif m == "Energy.Active.Import.Register":
                            paal.meter_wh = v * factor
                            if paal.sessie_start_wh is None:
                                paal.sessie_start_wh = paal.meter_wh
                return call_result.MeterValues()

        async def verbinding(ws):
            naam = ws.request.path.strip("/") or "laadpaal"
            paal.cp = Paal(naam, ws)
            log.info("Laadpaal %s verbonden", naam)
            try:
                await paal.cp.start()
            except ConnectionClosed:
                log.info("Laadpaal %s verbinding verbroken", naam)
            finally:
                paal.cp, paal.verbonden, paal.kw, paal._laatste_a = None, False, 0.0, None

        self.server = await serve(verbinding, "0.0.0.0", self.cfg["poort"], subprotocols=["ocpp1.6"])
        log.info("OCPP-server luistert op poort %s", self.cfg["poort"])

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def lees(self) -> dict:
        geladen = (self.meter_wh - self.sessie_start_wh) / 1000 if self.sessie_start_wh is not None else 0.0
        return {"auto_kw": self.kw, "auto_verbonden": self.verbonden, "auto_geladen_kwh": max(geladen, 0.0)}

    async def laadstroom(self, ampere: float):
        if self.cp is None or ampere == self._laatste_a:
            return
        from ocpp.v16 import call
        from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType
        profiel = {"charging_profile_id": 1, "stack_level": 0,
                   "charging_profile_purpose": ChargingProfilePurposeType.tx_default_profile,
                   "charging_profile_kind": ChargingProfileKindType.relative,
                   "charging_schedule": {"charging_rate_unit": ChargingRateUnitType.amps,
                                         "charging_schedule_period": [{"start_period": 0, "limit": float(ampere)}]}}
        await self.cp.call(call.SetChargingProfile(connector_id=1, cs_charging_profiles=profiel))
        self._laatste_a = ampere
        log.info("Laadpaal: %s A", ampere)


# ======================= Warmtepomp via SG-ready (2 Shelly-relais) =======================
class SGReadyShelly:
    """SG-ready: met twee contacten zeg je tegen de warmtepomp: normaal, wacht (geblokkeerd) of stook extra.
    Stand 1 (wacht) = 1/0, stand 2 (normaal) = 0/0, stand 3 (extra aanbevolen) = 0/1."""
    STANDEN = {"wacht": (True, False), "normaal": (False, False), "boost": (False, True)}

    def __init__(self, cfg: dict):
        self.cfg = cfg["warmtepomp"]
        self.stand = "normaal"
        self._laatste = None

    async def start(self):
        await self.warmtepomp("normaal")          # beginnen in de gewone stand

    async def stop(self):
        """Nooit een geblokkeerde warmtepomp achterlaten: bij afsluiten terug naar normaal."""
        try:
            self._laatste = None
            await self.warmtepomp("normaal")
        except Exception as e:
            log.warning("Warmtepomp terugzetten naar normaal mislukt: %s", e)

    async def _relais(self, s: aiohttp.ClientSession, host: str, aan: bool):
        # Shelly Gen2/Gen3 RPC. "192.168.2.40/1" = kanaal 1 van een Shelly met meer relais (zoals de Pro 2 of Pro 3).
        host, _, kanaal = host.strip().partition("/")
        async with s.get(f"http://{host}/rpc/Switch.Set", params={"id": int(kanaal or 0), "on": "true" if aan else "false"}) as r:
            r.raise_for_status()

    async def warmtepomp(self, stand: str):
        if stand == self._laatste:
            return
        r1, r2 = self.STANDEN.get(stand, (False, False))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            await self._relais(s, self.cfg["relais1"], r1)
            await self._relais(s, self.cfg["relais2"], r2)
        self._laatste = self.stand = stand
        log.info("Warmtepomp: %s", stand)

    async def lees(self) -> dict:
        meter = (self.cfg.get("meter") or "").strip()
        if meter:
            kw = await shelly_vermogen_kw(meter)
            return {"wp_kw": kw, "wp_stand": self.stand}
        # Zonder eigen meter schatten we het vermogen uit de stand
        p = self.cfg["vermogen_kw"]
        return {"wp_kw": {"boost": p, "wacht": 0.0}.get(self.stand, p * 0.35), "wp_stand": self.stand}


async def shelly_vermogen_kw(host: str) -> float:
    """Vermogen (kW) van een tussenmeter op de groep van de warmtepomp.
    Werkt met de HomeWizard kWh-meter (1- en 3-fase, lokale API aanzetten in de HomeWizard-app),
    Shelly Pro 3EM / 3EM-63 (driefasen), Pro EM / EM Gen3 en de oudere Shelly EM / 3EM."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
        try:                                                 # HomeWizard kWh-meter
            async with s.get(f"http://{host}/api/v1/data") as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    if "active_power_w" in d:
                        return abs(float(d["active_power_w"] or 0)) / 1000
        except (aiohttp.ClientError, ValueError):
            pass
        for pad in ("/rpc/EM.GetStatus?id=0", "/rpc/EM1.GetStatus?id=0", "/status"):
            try:
                async with s.get(f"http://{host}{pad}") as r:
                    if r.status != 200:
                        continue
                    d = await r.json(content_type=None)
            except aiohttp.ClientError:
                continue
            if "total_act_power" in d:                       # Pro 3EM
                return abs(float(d["total_act_power"])) / 1000
            if "act_power" in d:                             # EM1 (één kanaal)
                return abs(float(d["act_power"])) / 1000
            if "emeters" in d:                               # Gen1 EM / 3EM
                return abs(sum(float(e.get("power") or 0) for e in d["emeters"])) / 1000
    raise RuntimeError(f"geen HomeWizard- of Shelly-energiemeter gevonden op {host}. Staat bij HomeWizard de lokale API aan?")


# ======================= hulp voor eenvoudige HTTP-apparaten =======================
class _Http:
    kan_sturen = False

    def __init__(self, host: str):
        self.host = host
        self.sessie: aiohttp.ClientSession | None = None

    @classmethod
    def voor_test(cls, ip, extra):
        return cls(ip)

    async def start(self):
        self.sessie = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def stop(self):
        if self.sessie:
            await self.sessie.close()

    async def _get(self, pad: str):
        async with self.sessie.get(f"http://{self.host}{pad}") as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    # omvormers zonder stuurkoppeling: opdrachten worden genegeerd (en gemeld in de app)
    async def accu(self, *a):
        raise NotImplementedError("Aansturen van de accu is voor dit merk nog niet gekoppeld; het brein leest alleen uit.")

    async def exportlimiet(self, kw):
        if kw == 0:
            raise NotImplementedError("Aftoppen is voor dit merk nog niet gekoppeld.")


# ======================= Fronius (Solar API, lokaal) =======================
class Fronius(_Http):
    """Fronius Solar API v1: vermogens van zon, net en accu (Symo, Primo, Gen24)."""

    def __init__(self, cfg_of_host):
        super().__init__(cfg_of_host if isinstance(cfg_of_host, str) else cfg_of_host["omvormer"]["host"])

    async def lees(self) -> dict:
        d = await self._get("/solar_api/v1/GetPowerFlowRealtimeData.fcgi")
        site = d["Body"]["Data"]["Site"]
        soc = 0.0
        for inv in (d["Body"]["Data"].get("Inverters") or {}).values():
            if inv.get("SOC") is not None:
                soc = float(inv["SOC"])
        uit = {"zon_kw": (site.get("P_PV") or 0) / 1000, "accu_kw": -(site.get("P_Akku") or 0) / 1000, "accu_soc": soc}
        if site.get("P_Grid") is not None:
            uit["net_kw"] = site["P_Grid"] / 1000      # Fronius: + = afname
        return uit


# ======================= SunSpec Modbus TCP (SolarEdge, SMA, Fronius) =======================
class SunSpec:
    """Leest het AC-vermogen van een SunSpec-omvormer via Modbus TCP. Aansturen volgt (verschilt per merk)."""
    kan_sturen = False

    def __init__(self, cfg_of_host, poort: int = 502, unit: int = 1):
        if isinstance(cfg_of_host, dict):
            o = cfg_of_host["omvormer"]
            self.host, self.poort, self.unit = o["host"], o.get("modbus_poort", 502), o.get("modbus_unit", 1)
        else:
            self.host, self.poort, self.unit = cfg_of_host, poort, unit
        self.c = None
        self.adres_w = None   # registeradres van W in model 101/102/103

    @classmethod
    def voor_test(cls, ip, extra):
        return cls(ip, int(extra.get("modbus_poort", 502)), int(extra.get("modbus_unit", 1)))

    async def start(self):
        from pymodbus.client import AsyncModbusTcpClient
        self.c = AsyncModbusTcpClient(self.host, port=self.poort, timeout=3)
        if not await self.c.connect():
            raise ConnectionError(f"Modbus op {self.host}:{self.poort} niet bereikbaar. Staat Modbus TCP aan op de omvormer?")
        r = await self.c.read_holding_registers(40000, count=2, device_id=self.unit)
        if r.isError() or r.registers != [0x5375, 0x6E53]:
            raise ConnectionError("Geen SunSpec-omvormer gevonden op dit adres (verkeerde unit-id?).")
        adres = 40002
        for _ in range(30):   # modellen aflopen tot het omvormermodel
            r = await self.c.read_holding_registers(adres, count=2, device_id=self.unit)
            mid, lengte = r.registers
            if mid == 0xFFFF:
                break
            if mid in (101, 102, 103):
                self.adres_w = adres + 2 + 12
                break
            adres += 2 + lengte
        if self.adres_w is None:
            raise ConnectionError("SunSpec-omvormermodel niet gevonden.")

    async def stop(self):
        if self.c:
            self.c.close()

    async def lees(self) -> dict:
        if self.adres_w is None:   # bij opstarten niet gelukt: opnieuw verbinden
            await self.start()
        r = await self.c.read_holding_registers(self.adres_w, count=2, device_id=self.unit)
        w, sf = r.registers
        w = w - 65536 if w > 32767 else w
        sf = sf - 65536 if sf > 32767 else sf
        return {"zon_kw": max(w * 10 ** sf, 0) / 1000}

    async def accu(self, *a):
        raise NotImplementedError("Aansturen via SunSpec volgt; het brein leest nu alleen uit.")

    async def exportlimiet(self, kw):
        if kw == 0:
            raise NotImplementedError("Aftoppen via SunSpec volgt.")


# ======================= slimme meters =======================
class Youless(_Http):
    def __init__(self, cfg_of_host):
        super().__init__(cfg_of_host if isinstance(cfg_of_host, str) else cfg_of_host["net"]["p1"]["host"])

    async def lees(self) -> dict:
        d = await self._get("/a?f=j")
        return {"net_kw": float(d["pwr"]) / 1000}


class P1Monitor(_Http):
    def __init__(self, cfg_of_host):
        super().__init__(cfg_of_host if isinstance(cfg_of_host, str) else cfg_of_host["net"]["p1"]["host"])

    async def lees(self) -> dict:
        d = (await self._get("/api/v1/smartmeter?limit=1&json=object"))[0]
        return {"net_kw": (float(d["CONSUMPTION_W"]) - float(d["PRODUCTION_W"])) / 1000}


class ShellyEM(_Http):
    """Shelly Pro 3EM (Gen2) of 3EM (Gen1) als netmeter op de hoofdaansluiting."""

    def __init__(self, cfg_of_host):
        super().__init__(cfg_of_host if isinstance(cfg_of_host, str) else cfg_of_host["net"]["p1"]["host"])

    async def lees(self) -> dict:
        try:
            d = await self._get("/rpc/EM.GetStatus?id=0")
            return {"net_kw": float(d["total_act_power"]) / 1000}
        except Exception:
            d = await self._get("/status")
            return {"net_kw": sum(float(e["power"]) for e in d["emeters"]) / 1000}


# ======================= go-e Charger (lokale API v2) =======================
class GoE(_Http):
    kan_sturen = True

    def __init__(self, cfg_of_host):
        if isinstance(cfg_of_host, str):
            super().__init__(cfg_of_host)
        else:
            super().__init__(cfg_of_host["laadpaal"]["host"])
        self._laatste = None

    async def lees(self) -> dict:
        d = await self._get("/api/status?filter=nrg,car,wh")
        car = int(d.get("car") or 0)   # 1 geen auto, 2 laadt, 3 wacht, 4 klaar
        return {"auto_kw": float(d["nrg"][11]) / 1000, "auto_verbonden": car in (2, 3, 4),
                "auto_geladen_kwh": float(d.get("wh") or 0) / 1000}

    async def laadstroom(self, ampere: float):
        a = int(ampere)
        if a == self._laatste:
            return
        pad = "/api/set?frc=1" if a <= 0 else f"/api/set?frc=0&amp={min(max(a, 6), 32)}"
        async with self.sessie.get(f"http://{self.host}{pad}") as r:
            r.raise_for_status()
        self._laatste = a
        log.info("go-e: %s", "pauze" if a <= 0 else f"{a} A")

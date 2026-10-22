"""Scant je thuisnetwerk en herkent slimme energie-apparaten.

Drie manieren tegelijk:
1. mDNS: apparaten die zichzelf aankondigen (HomeWizard, Shelly, Enphase, ESPHome, ...)
2. Een rondgang langs alle adressen in je netwerk: welke poorten staan open (web 80, Modbus 502/1502),
   daarna per apparaat herkennen aan zijn eigen lokale API of webpagina
3. UDP-broadcast voor wifi-modules van GoodWe en Solarman (Growatt, Solis, Deye, Sofar)

Er wordt alleen gelezen; er wordt nergens ingelogd of iets veranderd.
"""
from __future__ import annotations
import asyncio
import ipaddress
import logging
import re
import socket

import aiohttp

log = logging.getLogger("zoeken")

MDNS_TYPES = ["_hwenergy._tcp.local.", "_shelly._tcp.local.", "_enphase-envoy._tcp.local.", "_esphomelib._tcp.local.",
              "_openevse._tcp.local.", "_http._tcp.local."]
MDNS_NAMEN = [("homewizard", "p1", "HomeWizard"), ("p1meter", "p1", "HomeWizard"), ("envoy", "omvormer", "Enphase IQ Gateway"),
              ("enphase", "omvormer", "Enphase IQ Gateway"), ("slimmelezer", "p1", "SlimmeLezer / ESPHome"),
              ("go-echarger", "laadpaal", "go-e Charger"), ("peblar", "laadpaal", "Peblar"), ("fronius", "omvormer", "Fronius"),
              ("victron", "omvormer", "Victron (GX)"), ("venus", "omvormer", "Victron (GX)"), ("sessy", "omvormer", "Sessy"),
              ("openevse", "laadpaal", "OpenEVSE"), ("alfen", "laadpaal", "Alfen Eve")]
WEB_TREFWOORDEN = [("alfen", "laadpaal", "Alfen Eve"), ("victron", "omvormer", "Victron (GX)"), ("sungrow", "omvormer", "Sungrow"),
                   ("winet", "omvormer", "Sungrow"), ("solaredge", "omvormer", "SolarEdge"), ("sunny", "omvormer", "SMA"),
                   ("sma solar", "omvormer", "SMA"), ("huawei", "omvormer", "Huawei SUN2000 / LUNA"), ("sdongle", "omvormer", "Huawei SUN2000 / LUNA"),
                   ("peblar", "laadpaal", "Peblar"), ("wallbox", "laadpaal", "Wallbox"), ("sessy", "omvormer", "Sessy"),
                   ("daikin", "warmtepomp", "Daikin Altherma"), ("envoy", "omvormer", "Enphase IQ Gateway"),
                   ("growatt", "omvormer", "Growatt / Solis / Deye / Sofar"), ("solarman", "omvormer", "Growatt / Solis / Deye / Sofar"),
                   ("easee", "laadpaal", "Easee"), ("zaptec", "laadpaal", "Zaptec"), ("abl", "laadpaal", "ABL")]


def _s16(v):
    """GoodWe geeft sommige stromen als 16-bits getal zonder teken: 65496 betekent -40."""
    try:
        v = int(v)
    except Exception:
        return v
    return v - 65536 if v > 32767 else v


def eigen_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 80))   # verstuurt niets; geeft het eigen adres in het thuisnetwerk
            return s.getsockname()[0]
    except OSError:
        return None


def _vondst(soort, merk, ip, naam="", koppel=None, via=""):
    return {"soort": soort, "merk": merk, "ip": ip, "naam": naam or merk, "koppel": koppel, "via": via}


# ---------------------------------------------------------------- 1. mDNS
async def _mdns(duur: float = 4.0) -> list[dict]:
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return []
    gezien: list[tuple[str, str]] = []
    try:
        azc = AsyncZeroconf()

        def handler(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                gezien.append((service_type, name))

        browser = AsyncServiceBrowser(azc.zeroconf, MDNS_TYPES, handlers=[handler])
        await asyncio.sleep(duur)
        uit = []
        for stype, naam in gezien:
            info = AsyncServiceInfo(stype, naam)
            if not await info.async_request(azc.zeroconf, 1500):
                continue
            ips = info.parsed_addresses()
            if not ips:
                continue
            laag = naam.lower()
            if stype.startswith("_hwenergy"):
                prod = (info.properties or {}).get(b"product_type", b"").decode()
                uit.append(_vondst("p1" if prod in ("HWE-P1", "") else "meter", "HomeWizard", ips[0], naam.split(".")[0],
                                   {"p1": "homewizard"} if prod in ("HWE-P1", "") else None, "mDNS"))
            elif stype.startswith("_shelly"):
                uit.append(_vondst("relais", "Shelly", ips[0], naam.split(".")[0], None, "mDNS"))
            elif stype.startswith("_enphase"):
                uit.append(_vondst("omvormer", "Enphase IQ Gateway", ips[0], naam.split(".")[0], None, "mDNS"))
            elif stype.startswith("_esphomelib"):
                if any(w in laag for w in ("slimme", "p1", "dsmr")):
                    uit.append(_vondst("p1", "SlimmeLezer / ESPHome", ips[0], naam.split(".")[0], None, "mDNS"))
            else:
                for woord, soort, merk in MDNS_NAMEN:
                    if woord in laag:
                        uit.append(_vondst(soort, merk, ips[0], naam.split(".")[0], None, "mDNS"))
                        break
        await browser.async_cancel()
        await azc.async_close()
        return uit
    except Exception as e:
        log.info("mDNS niet beschikbaar: %s", e)
        return []


# ---------------------------------------------------------------- 3. UDP (GoodWe / Solarman)
async def _udp_dongles() -> list[dict]:
    try:
        import goodwe
        antwoord = await asyncio.wait_for(goodwe.search_inverters(), timeout=4)
    except Exception as e:
        log.info("Geen wifi-module gevonden via broadcast (%s)", e)
        return []
    uit = []
    for regel in antwoord.decode("utf-8", "ignore").splitlines() or [antwoord.decode("utf-8", "ignore")]:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", regel)
        if not m:
            continue
        delen = [d.strip() for d in regel.split(",")]
        naam = delen[-1] if len(delen) > 2 else ""
        if "solar-wifi" in regel.lower() or "goodwe" in regel.lower() or not naam:
            uit.append(_vondst("omvormer", "GoodWe", m.group(1), naam or "GoodWe-omvormer", {"omvormer": "goodwe"}, "UDP"))
        else:
            uit.append(_vondst("omvormer", "Growatt / Solis / Deye / Sofar", m.group(1), naam, None, "UDP"))
    return uit


# ---------------------------------------------------------------- 2. adressen rondgaan
async def _poort_open(ip: str, poort: int, timeout: float = 0.6) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, poort), timeout)
        w.close()
        return True
    except Exception:
        return False


async def _json(s: aiohttp.ClientSession, url: str):
    try:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def _web(s: aiohttp.ClientSession, ip: str, poort: int = 80) -> dict | None:
    base = f"http://{ip}" if poort == 80 else f"http://{ip}:{poort}"
    d = await _json(s, base + "/api")
    if isinstance(d, dict) and str(d.get("product_type", "")).startswith("HWE"):
        p1 = d["product_type"] == "HWE-P1"
        return _vondst("p1" if p1 else "meter", "HomeWizard", ip, d.get("product_name", d["product_type"]), {"p1": "homewizard"} if p1 else None, "web")
    d = await _json(s, base + "/shelly")
    if isinstance(d, dict) and ("gen" in d or "type" in d):
        model = str(d.get("model") or d.get("type") or "")
        naam = d.get("name") or model or "Shelly"
        if "EM" in model.upper():
            return _vondst("p1", "Shelly Pro 3EM / EM", ip, naam, {"p1": "shelly_em"}, "web")
        return _vondst("relais", "Shelly", ip, naam, None, "web")
    d = await _json(s, base + "/solar_api/GetAPIVersion.cgi")
    if isinstance(d, dict) and "APIVersion" in d:
        return _vondst("omvormer", "Fronius", ip, "Fronius-omvormer", {"omvormer": "fronius"}, "web")
    d = await _json(s, base + "/api/status?filter=car,amp,fna")
    if isinstance(d, dict) and "car" in d and "amp" in d:
        return _vondst("laadpaal", "go-e Charger", ip, d.get("fna") or "go-e Charger", {"laadpaal": "goe"}, "web")
    d = await _json(s, base + "/api/1/vitals")
    if isinstance(d, dict) and "vehicle_connected" in d:
        return _vondst("laadpaal", "Tesla Wall Connector", ip, "Tesla Wall Connector", None, "web")
    d = await _json(s, base + "/a?f=j")
    if isinstance(d, dict) and "pwr" in d and "cnt" in d:
        return _vondst("p1", "Youless", ip, "Youless", {"p1": "youless"}, "web")
    d = await _json(s, base + "/api/v1/smartmeter?limit=1&json=object")
    if isinstance(d, list) and d and isinstance(d[0], dict) and "CONSUMPTION_W" in d[0]:
        return _vondst("p1", "P1 Monitor", ip, "P1 Monitor", {"p1": "p1monitor"}, "web")
    # laatste poging: de webpagina zelf
    try:
        async with s.get(base + "/") as r:
            tekst = (await r.text(errors="ignore"))[:6000].lower()
        for woord, soort, merk in WEB_TREFWOORDEN:
            if woord in tekst:
                return _vondst(soort, merk, ip, merk, None, "web")
    except Exception:
        pass
    return None


async def _modbus(ip: str, poort: int) -> dict | None:
    """SunSpec-omvormers (SolarEdge, SMA, Fronius) melden 'SunS' op register 40000. Huawei meldt zijn model op 30000."""
    try:
        from pymodbus.client import AsyncModbusTcpClient
    except ImportError:
        return _vondst("omvormer", "Modbus-apparaat", ip, f"Modbus op poort {poort}", None, "Modbus")
    c = AsyncModbusTcpClient(ip, port=poort, timeout=2, retries=0)
    try:
        if not await c.connect():
            return None
        for unit in (1, 126, 3):
            try:
                r = await c.read_holding_registers(40000, count=2, device_id=unit)
                if not r.isError() and r.registers == [0x5375, 0x6E53]:
                    m = await c.read_holding_registers(40004, count=16, device_id=unit)
                    fabr = b"".join(v.to_bytes(2, "big") for v in m.registers).decode("ascii", "ignore").strip("\x00 ") if not m.isError() else ""
                    merk = "SolarEdge" if "solaredge" in fabr.lower() else "SMA" if "sma" in fabr.lower() else "Fronius" if "fronius" in fabr.lower() else fabr or "SunSpec-omvormer"
                    soort = {"SolarEdge": "solaredge", "SMA": "sma", "Fronius": "fronius"}.get(merk, "sunspec")
                    return _vondst("omvormer", merk, ip, f"{merk} (SunSpec, unit {unit})",
                                   {"omvormer": soort, "modbus_poort": poort, "modbus_unit": unit}, "Modbus")
            except Exception:
                continue
        for unit in (1, 0):
            try:
                r = await c.read_holding_registers(30000, count=15, device_id=unit)
                if not r.isError():
                    tekst = b"".join(v.to_bytes(2, "big") for v in r.registers).decode("ascii", "ignore")
                    if "SUN2000" in tekst.upper():
                        return _vondst("omvormer", "Huawei SUN2000 / LUNA", ip, tekst.strip("\x00 "),
                                       {"omvormer": "huawei", "modbus_poort": poort, "modbus_unit": unit}, "Modbus")
            except Exception:
                continue
        return _vondst("omvormer", "Modbus-apparaat", ip, f"Modbus op poort {poort} (merk onbekend)", None, "Modbus")
    finally:
        c.close()


async def _rondgang(netwerk: ipaddress.IPv4Network, eigen: str | None) -> list[dict]:
    hosts = [str(ip) for ip in netwerk.hosts() if str(ip) != eigen]
    rem = asyncio.Semaphore(128)

    async def scan(ip):
        async with rem:
            poorten = [p for p in (80, 502, 1502) if await _poort_open(ip, p)]
            return ip, poorten

    open_ = [x for x in await asyncio.gather(*(scan(ip) for ip in hosts)) if x[1]]
    uit = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5)) as s:
        async def herken(ip, poorten):
            async with rem:
                v = await _web(s, ip) if 80 in poorten else None
                modbus = [p for p in (1502, 502) if p in poorten]
                if v and (v["koppel"] or not modbus):
                    return v
                for p in modbus:
                    v2 = await _modbus(ip, p)
                    if v2 and (v2["koppel"] or not v):
                        return v2
                return v
        uit = [v for v in await asyncio.gather(*(herken(ip, p) for ip, p in open_)) if v]
    return uit


async def zoek(netwerk: str | None = None) -> dict:
    eigen = eigen_ip()
    net = ipaddress.ip_network(netwerk or f"{eigen}/24", strict=False) if (netwerk or eigen) else None
    taken = [_mdns(), _udp_dongles()]
    if net:
        taken.append(_rondgang(net, eigen))
    resultaten = await asyncio.gather(*taken)
    # samenvoegen: per IP het meest specifieke resultaat (met koppeling) houden
    per_ip: dict[str, dict] = {}
    for lijst in resultaten:
        for v in lijst:
            oud = per_ip.get(v["ip"])
            if not oud or (v["koppel"] and not oud["koppel"]) or (oud["merk"] in ("Modbus-apparaat", "Shelly") and v["merk"] not in ("Modbus-apparaat",)):
                per_ip[v["ip"]] = v
    standaard = {"Victron (GX)": {"omvormer": "victron"}, "Sungrow": {"omvormer": "sungrow"}, "Huawei SUN2000 / LUNA": {"omvormer": "huawei"},
                 "Enphase IQ Gateway": {"omvormer": "enphase"}, "Sessy": {"omvormer": "sessy"}, "Growatt / Solis / Deye / Sofar": {"omvormer": "deye"},
                 "SlimmeLezer / ESPHome": {"p1": "esphome"}, "Tesla Wall Connector": {"laadpaal": "tesla"},
                 "SolarEdge": {"omvormer": "solaredge", "modbus_poort": 1502}, "SMA": {"omvormer": "sma", "modbus_unit": 3},
                 "Fronius": {"omvormer": "fronius"}, "Easee": {"laadpaal": "easee"}}
    for v in per_ip.values():
        if not v["koppel"] and v["merk"] in standaard:
            v["koppel"] = standaard[v["merk"]]
    volgorde = {"omvormer": 0, "p1": 1, "laadpaal": 2, "warmtepomp": 3, "relais": 4, "meter": 5}
    gevonden = sorted(per_ip.values(), key=lambda v: (volgorde.get(v["soort"], 9), v["merk"]))
    return {"eigen_ip": eigen, "netwerk": str(net) if net else None, "gevonden": gevonden}


# ---------------------------------------------------------------- testen
async def test(soort: str, ip: str, extra: dict | None = None) -> dict:
    """Probeert één keer te lezen met de echte driver. Geeft een begrijpelijke uitkomst terug."""
    from .apparaten import echt
    extra = extra or {}
    try:
        if soort == "goodwe":
            inv = await asyncio.wait_for(echt.goodwe_verbind(ip, extra.get("goodwe_familie")), timeout=150)
            d = await inv.read_runtime_data()
            g = echt.GoodWe({"omvormer": {"host": ip}, "accu": {}})
            g.inv = inv
            st = await g.lees_stand()
            extra_tekst = f", werkmodus {st.get('werkmodus')}" + (f", EMS-modus {st['ems']}" if st.get("ems") else "")
            if st.get("ondergrens_pct") is not None:
                extra_tekst += f", ontlaadt tot {st['ondergrens_pct']}%"
            # gegevens van de accu zelf (BMS): waarom levert hij wel of niet
            accu_delen = []
            if d.get("battery_mode_label"):
                accu_delen.append(f"accustand {d['battery_mode_label']}")
            if d.get("battery_discharge_limit") is not None:
                accu_delen.append(f"accu mag ontladen {abs(_s16(d['battery_discharge_limit']))} A")
            if d.get("battery_charge_limit") is not None:
                accu_delen.append(f"laden {d['battery_charge_limit']} A")
            if d.get("battery_temperature") is not None:
                accu_delen.append(f"accutemperatuur {d['battery_temperature']} °C")
            for sleutel, naam in (("battery_error", "accufout"), ("battery_warning", "accuwaarschuwing")):
                w = d.get(sleutel)
                if w not in (None, "", 0, "0"):
                    accu_delen.append(f"{naam}: {w}")
            if d.get("warning_code") not in (None, 0):
                accu_delen.append(f"waarschuwingscode {d['warning_code']}")
            if accu_delen:
                extra_tekst += " | Accu: " + ", ".join(accu_delen)
            meter = d.get("meter_active_power_total", d.get("active_power"))
            if meter is not None:
                extra_tekst += f", eigen meter ziet {-float(meter) / 1000:.1f} kW {'afname' if float(meter) <= 0 else 'teruglevering'}"
            return {"ok": True, "tekst": f"{inv.model_name}: zon {(d.get('ppv') or 0) / 1000:.1f} kW, accu {d.get('battery_soc', '?')}%{extra_tekst}"}
        from .apparaten import merken as mk
        driver = {"fronius": echt.Fronius, "sunspec": echt.SunSpec, "homewizard": echt.HomeWizardP1, "youless": echt.Youless,
                  "p1monitor": echt.P1Monitor, "shelly_em": echt.ShellyEM, "goe": echt.GoE, "solaredge": mk.SolarEdge,
                  "sma": mk.SMA, "huawei": mk.Huawei, "victron": mk.Victron, "sungrow": mk.Sungrow, "sessy": mk.Sessy,
                  "enphase": mk.Enphase, "deye": mk.DeyeSolarman, "esphome": mk.ESPHomeP1, "tibber": mk.TibberPulse,
                  "tesla": mk.TeslaWallConnector, "easee": mk.Easee}.get(soort)
        if soort == "wpmeter":
            from .apparaten.echt import shelly_vermogen_kw
            kw = await asyncio.wait_for(shelly_vermogen_kw(ip), timeout=10)
            return {"ok": True, "tekst": f"Warmtepomp gebruikt nu {kw:.2f} kW".replace(".", ",")}
        if soort == "shelly":
            ip = ip.partition("/")[0]          # "IP/kanaal" bij een Shelly met meer relais
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                d = await _json(s, f"http://{ip}/shelly")
            if not d:
                return {"ok": False, "tekst": f"Geen Shelly op {ip}"}
            return {"ok": True, "tekst": f"Shelly gevonden: {d.get('name') or d.get('model') or d.get('type')}"}
        if not driver:
            return {"ok": False, "tekst": "Onbekend apparaat"}
        d = driver.voor_test(ip, extra)
        await asyncio.wait_for(d.start(), timeout=20)
        try:
            w = await asyncio.wait_for(d.lees(), timeout=10)
        finally:
            await d.stop()
        delen = []
        if "zon_kw" in w: delen.append(f"zon {w['zon_kw']:.2f} kW")
        if "accu_soc" in w and w.get("accu_soc"): delen.append(f"accu {w['accu_soc']:.0f}%")
        if "net_kw" in w: delen.append(f"net {w['net_kw']:.2f} kW")
        if "auto_kw" in w: delen.append(f"laadpaal {w['auto_kw']:.1f} kW, auto {'aangesloten' if w.get('auto_verbonden') else 'niet aangesloten'}")
        return {"ok": True, "tekst": ", ".join(delen) or "Verbonden"}
    except asyncio.TimeoutError:
        return {"ok": False, "tekst": f"Geen antwoord van {ip}. Klopt het IP-adres en staat het apparaat aan?"}
    except PermissionError as e:
        return {"ok": False, "tekst": str(e)}
    except Exception as e:
        return {"ok": False, "tekst": f"Lukt niet: {e}" if str(e) else f"Geen verbinding met {ip}. Klopt het IP-adres en staat het apparaat aan?"}

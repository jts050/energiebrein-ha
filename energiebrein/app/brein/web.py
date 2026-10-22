"""De app: een webserver op het kastje. Open http://<ip-van-het-kastje>:8080 op je telefoon of iPad."""
from __future__ import annotations
import asyncio
import copy
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import vergelijk as vgl, zoeken
from .brein import Brein
from .config import BASIS, data_map

log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent / "app"

KEUZES = {"accu": {"auto", "laden", "ontladen", "vasthouden", "zelfverbruik"},
          "laadpaal": {"auto", "zon", "nu", "stop"},
          "warmtepomp": {"auto", "boost", "normaal", "wacht"},
          "zon": {"auto", "altijd"}}

# Wat je in de app mag instellen (de rest blijft zoals in config.yaml)
INSTELBAAR = {
    "simulatie": None,
    "locatie": ["breedte", "lengte"],
    "prijzen": ["bron", "entsoe_token", "tibber_token", "leverancier", "contract", "opslag", "terugleverkosten", "vast_inkoop", "vast_teruglever", "saldering"],
    "net": ["max_afname_kw", "p1"],
    "omvormer": ["vermogen_kw", "type", "host", "modbus_poort", "modbus_unit", "aftoppen_bij_negatieve_prijs", "meter_meet", "sturen", "gebruiker", "wachtwoord", "serienummer"],
    "zonnepanelen": None,
    "accu": ["capaciteit_kwh", "max_laden_kw", "max_ontladen_kw", "min_soc", "max_soc"],
    "laadpaal": ["type", "host", "fasen", "max_stroom_a", "auto_accu_kwh", "gebruiker", "wachtwoord", "serienummer"],
    "warmtepomp": ["type", "relais1", "relais2", "meter", "vermogen_kw", "jaarverbruik_kwh", "blokkeer_uren_per_dag", "max_wacht_achter_elkaar_uur", "niet_wachten_onder_c", "comfort_tijden"],
    "huis": ["jaarverbruik_kwh"],
    "auto": ["merk", "gebruiker", "wachtwoord", "spin", "vin"],
    "meldingen": ["telefoon", "bij_opgelost"],
    "toegang": ["pincode"],
}


class Keuze(BaseModel):
    apparaat: str
    stand: str


class AutoDoel(BaseModel):
    kwh: float
    vertrek: str


class Test(BaseModel):
    soort: str
    ip: str
    extra: dict = {}


def _deel(cfg: dict) -> dict:
    uit = {}
    for k, velden in INSTELBAAR.items():
        if velden is None:
            uit[k] = copy.deepcopy(cfg.get(k))
        else:
            uit[k] = {v: copy.deepcopy(cfg.get(k, {}).get(v)) for v in velden}
    return uit


def maak_app(cfg: dict, na_start=None, config_pad: Path | None = None) -> FastAPI:
    config_pad = Path(config_pad) if config_pad else BASIS / "config.yaml"
    staat = {"brein": Brein(cfg), "cfg": cfg}

    @asynccontextmanager
    async def levensduur(app):
        await staat["brein"].start()
        if na_start:
            na_start()
        yield
        await staat["brein"].stop()

    app = FastAPI(title="Energiebrein", lifespan=levensduur)
    from .toegang import Toegang
    toegang = Toegang(lambda: staat["cfg"])
    app.middleware("http")(toegang.middleware)
    app.post("/api/aanmelden")(toegang.aanmelden)
    B = lambda: staat["brein"]

    @app.get("/")
    async def index():
        # niet bewaren in de browser, anders zie je na een update nog de oude app
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/manifest.json")
    async def manifest():
        return FileResponse(STATIC / "manifest.json")

    @app.get("/icoon-{maat}.png")
    async def icoon(maat: int):
        if maat not in (192, 512):
            raise HTTPException(404)
        return FileResponse(STATIC / f"icoon-{maat}.png")

    @app.get("/api/status")
    async def status():
        return B().status()

    @app.get("/api/plan")
    async def plan():
        return {"kwartieren": [k.dict() for k in B().plan["kwartieren"]], "samenvatting": B().plan["samenvatting"]}

    @app.get("/api/geschiedenis")
    async def geschiedenis(uren: int = 24):
        return B().gesch.rijen(datetime.now(timezone.utc) - timedelta(hours=uren))

    @app.post("/api/bediening")
    async def bediening(k: Keuze):
        if k.apparaat not in KEUZES or k.stand not in KEUZES[k.apparaat]:
            raise HTTPException(400, f"Onbekende keuze: {k.apparaat} = {k.stand}")
        setattr(B().bed, k.apparaat, k.stand)
        B().bewaar_bediening()
        STAND = {"auto": "Slim (het brein beslist)", "laden": "laden", "ontladen": "leveren", "vasthouden": "vasthouden",
                 "zelfverbruik": "eigen verbruik", "zon": "alleen op zon", "nu": "nu laden", "stop": "stop",
                 "boost": "extra stoken", "normaal": "normaal", "wacht": "wachten", "altijd": "altijd terugleveren"}
        NAAM = {"accu": "Thuisaccu", "laadpaal": "Laadpaal", "warmtepomp": "Warmtepomp", "zon": "Zonnepanelen"}
        B().logboek.schrijf("handmatig", f"{NAAM[k.apparaat]} met de hand op {STAND.get(k.stand, k.stand)} gezet", k.apparaat)
        B().maak_plan()
        await B().regel()
        return B().status()

    @app.post("/api/auto")
    async def auto(d: AutoDoel):
        try:
            h, m = (int(x) for x in d.vertrek.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60 and 0 <= d.kwh <= 200
        except Exception:
            raise HTTPException(400, "Vertrektijd als UU:MM en kWh tussen 0 en 200")
        # oud kaartje: telt als eenmalige uitzondering voor het eerstvolgende moment op die tijd
        nu = datetime.now().astimezone()
        t = datetime(nu.year, nu.month, nu.day, h, m).astimezone()
        if t <= nu:
            morgen = nu.date() + timedelta(days=1)
            t = datetime(morgen.year, morgen.month, morgen.day, h, m).astimezone()
        B().autoplan.zet({"eenmalig": {"vertrek": t.isoformat(), "doel_soort": "kwh", "doel": d.kwh}})
        B().logboek.schrijf("handmatig", f"Auto: {d.kwh:.0f} kWh laden, klaar om {h:02d}:{m:02d}".replace(".", ","), "laadpaal")
        B().maak_plan()
        await B().regel()
        return B().status()

    @app.post("/api/auto/test")
    async def auto_test(d: dict):
        from .apparaten.autos import AutoAccount, MERKEN
        if d.get("merk") not in MERKEN:
            return {"ok": False, "tekst": "Kies eerst het merk van je auto."}
        a = AutoAccount({"auto": d}, data_map(staat["cfg"]))
        if not a.aan:
            return {"ok": False, "tekst": "Vul het e-mailadres en wachtwoord van het account van je auto in."}
        g = await a.ververs(forceer=True)
        a.stop()
        if a.fout:
            return {"ok": False, "tekst": a.fout[0].upper() + a.fout[1:] + "."}
        delen = [g.get("naam") or "Auto gevonden"]
        if g.get("soc") is not None:
            delen.append(f"{g['soc']:.0f}% geladen")
        if g.get("bereik_km") is not None:
            delen.append(f"{g['bereik_km']} km bereik")
        if g.get("aangesloten") is not None:
            delen.append("stekker erin" if g["aangesloten"] else "niet aangesloten")
        return {"ok": True, "tekst": ", ".join(delen)}

    @app.get("/api/autoplan")
    async def autoplan_lezen():
        return B().auto_overzicht()

    @app.post("/api/autoplan")
    async def autoplan_zetten(d: dict):
        try:
            B().autoplan.zet(d)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, f"Controleer de tijden en het doel ({e}).")
        ap = B().autoplan.d
        if "eenmalig" in d:
            e = ap.get("eenmalig")
            B().logboek.schrijf("handmatig", (f"Auto: eenmalig {e['doel']:.0f} {e['doel_soort']} klaar op "
                                              f"{datetime.fromisoformat(e['vertrek']).strftime('%d-%m %H:%M')}") if e else
                                "Auto: eenmalige uitzondering verwijderd", "laadpaal")
        else:
            B().logboek.schrijf("handmatig", "Auto: laadschema aangepast (" + ", ".join(f"{dag} {t}" for dag, t in ap["week"].items() if t)
                                + f"; doel {ap['doel_' + ap['doel_soort']]:.0f} {ap['doel_soort']})", "laadpaal")
        B().maak_plan()
        await B().regel()
        return B().auto_overzicht()

    # ---------------- koppelen ----------------
    @app.get("/api/instellingen")
    async def instellingen():
        return {"instellingen": _deel(staat["cfg"]), "eigen_ip": zoeken.eigen_ip(),
                "ocpp_poort": staat["cfg"]["laadpaal"].get("poort", 9000),
                "laadpaal_verbonden": bool(getattr(B().huis.laadpaal, "cp", None))}

    @app.get("/api/opbrengst")
    async def opbrengst_api(dagen: int = 30):
        from .opbrengst import opbrengst
        nu = datetime.now(timezone.utc)
        return opbrengst(B().gesch.rijen(nu - timedelta(days=dagen + 1), nu + timedelta(days=1)), B().prijzen, dagen)

    @app.get("/api/leveranciers")
    async def leveranciers():
        return vgl.lijst()

    @app.post("/api/tibber/test")
    async def tibber_test(d: dict):
        """Controleert een Tibber-code en laat zien welk huis en welke prijs erbij hoort."""
        import aiohttp
        token = (d.get("token") or "").strip()
        if not token:
            return {"ok": False, "tekst": "Vul eerst je Tibber-code in."}
        vraag = "{ viewer { name homes { address { address1 postalCode } features { realTimeConsumptionEnabled } currentSubscription { priceInfo { current { total startsAt } } } } } }"
        try:
            from .net import sessie
            async with sessie(25) as s:
                async with s.post("https://api.tibber.com/v1-beta/gql", json={"query": vraag},
                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}) as r:
                    if r.status in (400, 401, 403):
                        return {"ok": False, "tekst": "Tibber accepteert deze code niet. Maak een nieuwe aan op developer.tibber.com (Settings → Access token) en plak hem zonder spaties."}
                    data = await r.json(content_type=None)
        except Exception as e:
            from .net import uitleg
            return {"ok": False, "tekst": f"Tibber is niet bereikbaar: {uitleg(e)}. Controleer of dit apparaat internet heeft."}
        if data.get("errors"):
            return {"ok": False, "tekst": "Tibber gaf een fout: " + "; ".join(x.get("message", "") for x in data["errors"])}
        v = data["data"]["viewer"]
        homes = v.get("homes") or []
        if not homes:
            return {"ok": False, "tekst": "De code werkt, maar er staat geen huis met een contract op dit Tibber-account."}
        h = homes[0]
        prijs = (((h.get("currentSubscription") or {}).get("priceInfo") or {}).get("current") or {}).get("total")
        adres = (h.get("address") or {}).get("address1", "")
        pulse = (h.get("features") or {}).get("realTimeConsumptionEnabled")
        tekst = f"Gekoppeld met {v.get('name') or 'je account'}, {adres}."
        if prijs is not None:
            tekst += f" Je prijs nu: € {prijs:.2f}".replace(".", ",") + " per kWh."
        if pulse:
            tekst += " Er is een Tibber Pulse gevonden; die kun je bij Slimme meter kiezen."
        return {"ok": True, "tekst": tekst, "pulse": bool(pulse)}

    @app.get("/api/licentie")
    async def licentie_status():
        return B().licentie.status()

    @app.post("/api/licentie")
    async def licentie_invullen(d: dict):
        """Licentiecode invullen of weghalen, direct controleren en opslaan."""
        import re
        from .licentie import Licentie
        sleutel = re.sub(r"\s+", "", (d.get("sleutel") or "")).upper()
        if sleutel and not re.fullmatch(r"EB-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", sleutel):
            raise HTTPException(400, "Een licentiecode ziet eruit als EB-ABCD-EFGH-JKLM.")
        cfg = staat["cfg"]
        if not (cfg.get("licentie") or {}).get("server"):
            raise HTTPException(400, "Deze installatie is voor eigen gebruik: alle functies staan al aan, een code is niet nodig.")
        cfg.setdefault("licentie", {})["sleutel"] = sleutel
        lic = Licentie(cfg)
        gelukt = await lic.controleer(B().huis.onderdelen())
        B().licentie = lic
        await B().pas_licentie_toe()
        bestaand = yaml.safe_load(config_pad.read_text(encoding="utf-8")) if config_pad.exists() else {}
        bestaand = bestaand or {}
        bestaand.setdefault("licentie", {})["sleutel"] = sleutel
        bestaand["licentie"].setdefault("server", cfg["licentie"]["server"])
        config_pad.write_text("# Opgeslagen vanuit de app\n" + yaml.safe_dump(bestaand, allow_unicode=True, sort_keys=False), encoding="utf-8")
        st = lic.status()
        if not gelukt:
            st["tekst"] = f"De licentieserver is niet bereikbaar ({lic.fout}). De code is bewaard; het kastje probeert het later opnieuw."
        return st

    @app.post("/api/omvormer/ondergrens")
    async def ondergrens(d: dict):
        omv = B().huis.omvormer
        if not omv or not hasattr(omv, "zet_ondergrens"):
            raise HTTPException(400, "Deze omvormer ondersteunt het aanpassen van de ondergrens vanuit Energiebrein niet.")
        if B().huis.vergrendeld:
            raise HTTPException(403, "Instellingen in de omvormer wijzigen hoort bij Plus.")
        try:
            pct = await omv.zet_ondergrens(int(d.get("pct") or 10))
        except Exception as e:
            raise HTTPException(502, f"Aanpassen lukt niet: {e}")
        B().logboek.schrijf("handmatig", f"Ondergrens van de accu in de omvormer op {pct}% gezet", "omvormer")
        return {"ok": True, "tekst": f"De accu mag nu tot {pct}% ontladen.", "status": B().status()}

    @app.get("/api/telefoon")
    async def telefoon_info():
        from . import telefoon
        if not telefoon.beschikbaar():
            return {"beschikbaar": False, "diensten": []}
        try:
            return {"beschikbaar": True, "diensten": await telefoon.diensten()}
        except Exception as e:
            return {"beschikbaar": True, "diensten": [], "fout": str(e)}

    @app.post("/api/telefoon/test")
    async def telefoon_test(body: dict):
        from . import telefoon
        try:
            await telefoon.stuur(str(body.get("dienst") or ""), "Energiebrein", "Testmelding: je telefoon is gekoppeld.")
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/logboek")
    async def logboek(dagen: int = 7, soort: str = ""):
        return {"rijen": B().logboek.rijen(max(1, min(dagen, 120)), soort or None)}

    @app.get("/api/dak")
    async def dak_zoeken(postcode: str, huisnummer: str):
        from . import dak
        try:
            return await dak.vind_dak(postcode, huisnummer)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            from .net import uitleg
            raise HTTPException(502, f"De kaartgegevens zijn nu niet bereikbaar: {uitleg(e)}.")

    @app.get("/api/vergelijk")
    async def vergelijk(dagen: int = 30):
        nu = datetime.now(timezone.utc)
        return vgl.vergelijk(B().gesch.rijen(nu - timedelta(days=dagen), nu), staat["cfg"]["prijzen"])

    @app.post("/api/zoek")
    async def zoek():
        return await zoeken.zoek(os.environ.get("ENERGIEBREIN_NETWERK"))   # alleen voor testen een ander netwerk

    @app.post("/api/test")
    async def test(t: Test):
        return await zoeken.test(t.soort, t.ip.strip(), t.extra)

    @app.get("/api/catalogus")
    async def catalogus():
        from .catalogus import CATALOGUS, STATUS_TEKST
        return {"merken": CATALOGUS, "status": STATUS_TEKST}

    @app.post("/api/instellingen")
    async def opslaan(nieuw: dict):
        cfg = copy.deepcopy(staat["cfg"])
        for k, velden in INSTELBAAR.items():
            if k not in nieuw:
                continue
            if velden is None:
                cfg[k] = nieuw[k]
            else:
                for v in velden:
                    if v in nieuw[k]:
                        cfg.setdefault(k, {})[v] = nieuw[k][v]
        # eenvoudige controles, zodat een typfout het brein niet stopt
        try:
            assert cfg["accu"]["capaciteit_kwh"] > 0 and cfg["accu"]["max_laden_kw"] > 0 and cfg["accu"]["max_ontladen_kw"] > 0
            assert isinstance(cfg["zonnepanelen"], list)
            for vlak in cfg["zonnepanelen"]:
                assert vlak["kwp"] >= 0 and -180 <= vlak["richting"] <= 180 and 0 <= vlak["hoek"] <= 90
        except Exception:
            raise HTTPException(400, "Controleer de accu- en zonnepaneelgegevens: getallen moeten groter dan 0 zijn.")
        kwp = sum(float(v.get("kwp") or 0) for v in cfg["zonnepanelen"])
        if kwp > 60 or any(float(v.get("kwp") or 0) > 40 for v in cfg["zonnepanelen"]):
            raise HTTPException(400, f"Je zonnepanelen zijn samen {kwp:.1f} kWp. Dat is erg veel voor een woning (meestal 3 tot 15 kWp). "
                                     "Controleer per dakvlak het vermogen: aantal panelen × wattpiek ÷ 1000.".replace(".", ",", 1))
        if not (0 <= float(cfg["accu"].get("min_soc", 10)) < float(cfg["accu"].get("max_soc", 100)) <= 100):
            raise HTTPException(400, "Laden tot moet hoger zijn dan Ontladen tot (bijvoorbeeld 90% en 10%).")
        pin = str((cfg.get("toegang") or {}).get("pincode") or "").strip()
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 12):
            raise HTTPException(400, "De pincode moet uit 4 tot 12 cijfers bestaan.")
        if cfg["accu"]["capaciteit_kwh"] > 200:
            raise HTTPException(400, "De accucapaciteit is groter dan 200 kWh. Vul de capaciteit in kWh in, niet in Wh.")
        config_pad.write_text("# Opgeslagen vanuit de app\n" + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        # brein herstarten met de nieuwe instellingen
        oud = staat["brein"]
        await oud.stop()
        await asyncio.sleep(0.5)   # poorten (laadpaal) vrijgeven
        oud.logboek.schrijf("systeem", "Instellingen opgeslagen vanuit de app; het brein herstart")
        nieuw_brein = Brein(cfg)
        await nieuw_brein.start()
        staat["brein"], staat["cfg"] = nieuw_brein, cfg
        log.info("Instellingen opgeslagen en brein herstart")
        return {"ok": True, "status": nieuw_brein.status()}

    return app

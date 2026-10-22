"""Je dak automatisch vinden: adres → locatie (PDOK) → 3D-model van je dak (3D BAG) → dakvlakken met richting en helling.

Alle bronnen zijn gratis open data van de Nederlandse overheid:
- PDOK Locatieserver: postcode + huisnummer naar coördinaten
- 3D BAG (TU Delft / 3DGI): elk gebouw in 3D, met per dakvlak de richting (b3_azimut) en helling (b3_hellingshoek)
- PDOK Luchtfoto: actuele luchtfoto om de panelen te zien en te tellen
"""
from __future__ import annotations
import logging
import math
import re
import socket

import aiohttp

from .net import sessie, uitleg

log = logging.getLogger("dak")
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
DRIEDBAG = "https://api.3dbag.nl/collections/pand/items"
LUCHTFOTO = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
PANEEL_M2 = 1.95          # moderne module ± 1,76 × 1,1 m
PANEEL_WP = 430


def _punt(wkt: str) -> tuple[float, float]:
    x, y = re.findall(r"[-\d.]+", wkt)[:2]
    return float(x), float(y)


async def zoek_adres(s: aiohttp.ClientSession, postcode: str, huisnummer: str) -> dict:
    pc = postcode.replace(" ", "").upper()
    m = re.match(r"\s*(\d+)", huisnummer)
    if not re.fullmatch(r"\d{4}[A-Z]{2}", pc) or not m:
        raise ValueError("Vul een postcode (1234AB) en huisnummer in.")
    q = f"postcode:{pc} and huisnummer:{int(m.group(1))}"
    async with s.get(LOCATIESERVER, params={"q": q, "fq": "type:adres", "rows": 5}) as r:
        r.raise_for_status()
        docs = (await r.json(content_type=None))["response"]["docs"]
    if not docs:
        raise ValueError(f"Adres {pc} {huisnummer} niet gevonden. Klopt de postcode?")
    toevoeging = re.sub(r"^\s*\d+", "", huisnummer).strip().upper()
    doc = next((d for d in docs if (d.get("huisletter", "") + d.get("huisnummertoevoeging", "")).upper() == toevoeging), docs[0])
    lon, lat = _punt(doc["centroide_ll"])
    x, y = _punt(doc["centroide_rd"])
    return {"adres": doc.get("weergavenaam", ""), "lat": lat, "lon": lon, "x": x, "y": y}


def _veelhoek_opp(pts: list[tuple[float, float, float]]) -> float:
    """Oppervlakte van een (schuin) 3D-vlak met de vectorformule."""
    sx = sy = sz = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1, z1 = pts[i]
        x2, y2, z2 = pts[(i + 1) % n]
        sx += y1 * z2 - z1 * y2
        sy += z1 * x2 - x1 * z2
        sz += x1 * y2 - y1 * x2
    return 0.5 * math.sqrt(sx * sx + sy * sy + sz * sz)


def _binnen(px, py, ring) -> bool:
    binnen, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi:
            binnen = not binnen
        j = i
    return binnen


def richting_tekst(azimut: float) -> str:
    namen = ["noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest"]
    return namen[int(((azimut % 360) + 22.5) // 45) % 8]


def dakvlakken(feature: dict, transform: dict) -> dict:
    """Haalt uit een 3D BAG-gebouw (CityJSONFeature) de dakvlakken van LoD 2.2."""
    sc, tr = transform["scale"], transform["translate"]
    verts = [(v[0] * sc[0] + tr[0], v[1] * sc[1] + tr[1], v[2] * sc[2] + tr[2]) for v in feature["vertices"]]
    gebouw, voetafdruk, vlakken = {}, [], []
    for oid, obj in feature["CityObjects"].items():
        if obj.get("type") == "Building":
            gebouw = obj.get("attributes", {})
            for g in obj.get("geometry", []):
                if str(g.get("lod")) == "0":
                    voetafdruk = [verts[i] for i in g["boundaries"][0][0]]
        for g in obj.get("geometry", []):
            if str(g.get("lod")) != "2.2" or "semantics" not in g:
                continue
            sem = g["semantics"]
            waarden = sem["values"][0] if g["type"] == "Solid" else sem["values"]
            schillen = g["boundaries"][0] if g["type"] == "Solid" else g["boundaries"]
            for vlak, idx in zip(schillen, waarden):
                if idx is None:
                    continue
                s = sem["surfaces"][idx]
                if s.get("type") != "RoofSurface":
                    continue
                ring = [verts[i] for i in vlak[0]]
                opp = _veelhoek_opp(ring)
                helling = float(s.get("b3_hellingshoek") or 0.0)
                azimut = float(s["b3_azimut"]) if s.get("b3_azimut") is not None else 180.0
                vlakken.append({"opp_m2": round(opp, 1), "helling": round(helling), "azimut": round(azimut),
                                "ring": [[round(p[0], 2), round(p[1], 2)] for p in ring], "hoogte": round(max(p[2] for p in ring), 1)})
    return {"gebouw": gebouw, "voetafdruk": [[p[0], p[1]] for p in voetafdruk], "vlakken": vlakken}


def advies(vlakken: list[dict]) -> list[dict]:
    """Per dakvlak: is het geschikt en hoeveel panelen passen er ongeveer op."""
    uit = []
    for i, v in enumerate(sorted(vlakken, key=lambda v: -v["opp_m2"])):
        plat = v["helling"] < 10
        richting = 0 if plat else (v["azimut"] % 360) - 180   # 3D BAG: 0 = noord, met de klok mee; forecast.solar: 0 = zuid, −90 = oost, 90 = west
        bruikbaar = v["opp_m2"] * (0.5 if plat else 0.7)
        panelen = int(bruikbaar // PANEEL_M2)
        geschikt = panelen >= 2 and (plat or abs(richting) <= 120)
        uit.append({**v, "id": i + 1, "plat": plat, "richting": round(richting), "hoek": 13 if plat else v["helling"],
                    "kompas": "plat" if plat else richting_tekst(v["azimut"]), "max_panelen": panelen,
                    "geschikt": geschikt, "max_kwp": round(panelen * PANEEL_WP / 1000, 1)})
    return uit


def _foto(kader: list[float]) -> tuple[str, str]:
    foto = (f"{LUCHTFOTO}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=Actueel_orthoHR&STYLES=&CRS=EPSG:28992"
            f"&BBOX={kader[0]:.2f},{kader[1]:.2f},{kader[2]:.2f},{kader[3]:.2f}&WIDTH=800&HEIGHT=800&FORMAT=image/jpeg")
    return foto, foto.replace("Actueel_orthoHR", "Actueel_ortho25")


BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
DRIEDBAG_WFS = "https://data.3dbag.nl/api/BAG3D/wfs"
AANGENOMEN_HELLING = 35   # gangbare dakhelling in Nederland, als de echte helling niet bekend is


async def _get_json(url: str, params: dict | None = None, totaal: float = 30, pogingen=((True, 15), (False, 15))) -> dict:
    """GET met eerst IPv4 en daarna het normale netwerk (IPv4 of IPv6), zodat één haperende route niet alles blokkeert."""
    laatste = None
    for ipv4, verbinden in pogingen:
        try:
            async with sessie(totaal, verbinden, ipv4=ipv4) as s:
                async with s.get(url, params=params, headers={"Accept": "application/json"}) as r:
                    r.raise_for_status()
                    return await r.json(content_type=None)
        except Exception as e:
            laatste = e
            log.warning("%s mislukt (%s): %s", url.split("/")[2], "IPv4" if ipv4 else "normaal", uitleg(e))
    raise ConnectionError(uitleg(laatste))


async def _bag_pand(x: float, y: float) -> dict | None:
    """Het pand op dit adres uit de officiële BAG (PDOK): id, bouwjaar en plattegrond."""
    d = await _get_json(BAG_WFS, {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": "bag:pand",
                                  "outputFormat": "application/json", "srsName": "EPSG:28992", "count": 20,
                                  "bbox": f"{x - 3:.2f},{y - 3:.2f},{x + 3:.2f},{y + 3:.2f},EPSG:28992"}, totaal=25)
    beste, afstand = None, 1e9
    for f in d.get("features") or []:
        g = f.get("geometry") or {}
        ringen = g.get("coordinates") or []
        if g.get("type") == "MultiPolygon":
            ringen = max(ringen, key=lambda p: len(p[0]))
        if not ringen:
            continue
        ring = [(p[0], p[1]) for p in ringen[0]]
        if _binnen(x, y, ring):
            beste = (f, ring)
            break
        cx, cy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if math.hypot(cx - x, cy - y) < afstand:
            afstand, beste = math.hypot(cx - x, cy - y), (f, ring)
    if not beste:
        return None
    f, ring = beste
    pr = f.get("properties") or {}
    return {"id": str(pr.get("identificatie") or ""), "bouwjaar": pr.get("bouwjaar"), "ring": ring}


async def _driedbag(pand_id: str | None, x: float, y: float) -> dict:
    """3D-model uit de 3D BAG. Met het pand-id vragen we alleen dat ene gebouw op (veel kleiner en sneller)."""
    if pand_id:
        return await _get_json(f"{DRIEDBAG}/NL.IMBAG.Pand.{pand_id}", totaal=40)
    return await _get_json(DRIEDBAG, {"bbox": f"{x - 10:.2f},{y - 10:.2f},{x + 10:.2f},{y + 10:.2f}"}, totaal=40)


async def _driedbag_wfs(pand_id: str, ring: list) -> list[dict]:
    """Tweede route naar dezelfde 3D BAG-gegevens: de 2D-dakdelen (LoD 2.2) met richting en helling per dakdeel."""
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    d = await _get_json(DRIEDBAG_WFS, {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": "BAG3D:lod22",
                                       "outputFormat": "application/json", "srsName": "EPSG:28992", "count": 200,
                                       "bbox": f"{min(xs):.2f},{min(ys):.2f},{max(xs):.2f},{max(ys):.2f},EPSG:28992"},
                        totaal=30, pogingen=((True, 12),))
    vlakken = []
    for f in d.get("features") or []:
        pr = f.get("properties") or {}
        if pand_id and pand_id not in str(pr.get("identificatie", "")):
            continue
        if pr.get("b3_hellingshoek") is None:
            continue
        g = f.get("geometry") or {}
        polys = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            polys = [polys]
        for poly in polys:
            r = [(p[0], p[1]) for p in poly[0]]
            helling = float(pr["b3_hellingshoek"])
            opp = abs(sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1] for i in range(len(r)))) / 2
            opp /= max(math.cos(math.radians(helling)), 0.2)      # schuin vlak is groter dan zijn plattegrond
            vlakken.append({"opp_m2": round(opp, 1), "helling": round(helling), "azimut": round(float(pr.get("b3_azimut") or 180)),
                            "ring": [[round(p[0], 2), round(p[1], 2)] for p in r], "hoogte": round(float(pr.get("b3_h_max") or 0), 1)})
    return vlakken


def _rechthoek(ring: list) -> tuple:
    """Kleinste rechthoek om de plattegrond: hoeken, lange as (eenheidsvector), lengte en breedte."""
    beste = None
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        l = math.hypot(x2 - x1, y2 - y1)
        if l < 0.5:
            continue
        ux, uy = (x2 - x1) / l, (y2 - y1) / l
        a = [p[0] * ux + p[1] * uy for p in pts]
        b = [-p[0] * uy + p[1] * ux for p in pts]
        opp = (max(a) - min(a)) * (max(b) - min(b))
        if beste is None or opp < beste[0]:
            beste = (opp, ux, uy, min(a), max(a), min(b), max(b))
    _, ux, uy, a0, a1, b0, b1 = beste
    punt = lambda a, b: (a * ux - b * uy, a * uy + b * ux)
    if a1 - a0 < b1 - b0:                                       # zorg dat 'a' de lange kant is
        ux, uy, a0, a1, b0, b1 = -uy, ux, b0, b1, -a1, -a0
        punt = lambda a, b: (a * ux - b * uy, a * uy + b * ux)
    return punt, (ux, uy), (a0, a1, b0, b1)


def schatting(ring: list) -> list[list[dict]]:
    """Zadeldak geschat uit de plattegrond. Twee varianten: nok over de lange kant, of over de korte kant."""
    punt, (ux, uy), (a0, a1, b0, b1) = _rechthoek(ring)
    varianten = []
    for nok_lang in (True, False):
        vlakken = []
        if nok_lang:                          # nok evenwijdig aan de lange kant: vlakken kijken dwars daarop
            bm = (b0 + b1) / 2
            helften = [((a0, b0), (a1, b0), (a1, bm), (a0, bm), (0.0, -1.0)), ((a0, bm), (a1, bm), (a1, b1), (a0, b1), (0.0, 1.0))]
        else:
            am = (a0 + a1) / 2
            helften = [((a0, b0), (am, b0), (am, b1), (a0, b1), (-1.0, 0.0)), ((am, b0), (a1, b0), (a1, b1), (am, b1), (1.0, 0.0))]
        for *hoeken, (na, nb) in helften:
            r = [punt(a, b) for a, b in hoeken]
            # normaal van lokaal (a,b) naar kaart (x oost, y noord) en dan naar kompasrichting (0 = noord, met de klok mee)
            nx, ny = na * ux - nb * uy, na * uy + nb * ux
            azimut = math.degrees(math.atan2(nx, ny)) % 360
            opp = abs(sum(r[i][0] * r[(i + 1) % 4][1] - r[(i + 1) % 4][0] * r[i][1] for i in range(4))) / 2
            vlakken.append({"opp_m2": round(opp / math.cos(math.radians(AANGENOMEN_HELLING)), 1), "helling": AANGENOMEN_HELLING,
                            "azimut": round(azimut), "ring": [[round(p[0], 2), round(p[1], 2)] for p in r], "hoogte": 0})
        varianten.append(vlakken)
    return varianten


def _kader_om(ring: list, x: float, y: float, marge: float = 6) -> list[float]:
    xs = [p[0] for p in ring] or [x]
    ys = [p[1] for p in ring] or [y]
    k = [min(xs) - marge, min(ys) - marge, max(xs) + marge, max(ys) + marge]
    zijde = max(k[2] - k[0], k[3] - k[1], 30)
    cx, cy = (k[0] + k[2]) / 2, (k[1] + k[3]) / 2
    return [cx - zijde / 2, cy - zijde / 2, cx + zijde / 2, cy + zijde / 2]


async def vind_dak(postcode: str, huisnummer: str) -> dict:
    async with sessie(25) as s:
        a = await zoek_adres(s, postcode, huisnummer)
    x, y = a["x"], a["y"]
    pand, problemen = None, []
    try:
        pand = await _bag_pand(x, y)
    except ConnectionError as e:
        problemen.append(f"BAG: {e}")
    pid = pand["id"] if pand else None
    basis = {**a, "paneel_wp": PANEEL_WP, "bouwjaar": pand.get("bouwjaar") if pand else None, "pand_id": pid}

    # 1. 3D BAG: echte dakvlakken uit het 3D-model
    gekozen = None
    try:
        data = await _driedbag(pid, x, y)
        transform = (data.get("metadata") or {}).get("transform")
        features = data.get("features") or ([data["feature"]] if "feature" in data else [])
        if transform and features:
            dichtst = (1e9, None)
            for f in features:
                d = dakvlakken(f, transform)
                if not d["voetafdruk"]:
                    continue
                if _binnen(x, y, d["voetafdruk"]) or len(features) == 1:
                    gekozen = d
                    break
                cx = sum(p[0] for p in d["voetafdruk"]) / len(d["voetafdruk"])
                cy = sum(p[1] for p in d["voetafdruk"]) / len(d["voetafdruk"])
                if math.hypot(cx - x, cy - y) < dichtst[0]:
                    dichtst = (math.hypot(cx - x, cy - y), d)
            gekozen = gekozen or dichtst[1]
    except ConnectionError as e:
        problemen.append(f"3D BAG: {e}")
    except Exception as e:
        problemen.append(f"3D BAG: {e}")
    if gekozen and gekozen["vlakken"]:
        ring = gekozen["voetafdruk"] or (pand["ring"] if pand else [])
        kader = _kader_om(ring, x, y)
        foto, reserve = _foto(kader)
        return {**basis, "bouwjaar": gekozen["gebouw"].get("oorspronkelijkbouwjaar") or basis["bouwjaar"],
                "daktype": gekozen["gebouw"].get("b3_dak_type"), "kader": kader, "foto": foto, "foto_reserve": reserve,
                "vlakken": advies(gekozen["vlakken"]), "bron": "3D BAG"}

    # 2. Tweede route naar de 3D BAG
    if pand:
        try:
            vl = await _driedbag_wfs(pid, pand["ring"])
            if vl:
                kader = _kader_om(pand["ring"], x, y)
                foto, reserve = _foto(kader)
                return {**basis, "kader": kader, "foto": foto, "foto_reserve": reserve, "vlakken": advies(vl), "bron": "3D BAG (dakdelen)"}
        except Exception as e:
            problemen.append(f"3D BAG dakdelen: {e}")

    # 3. Schatting uit de plattegrond van het pand
    if pand and len(pand["ring"]) >= 4:
        kader = _kader_om(pand["ring"], x, y)
        foto, reserve = _foto(kader)
        varianten = [advies(v) for v in schatting(pand["ring"])]
        log.info("Dak geschat uit de plattegrond; %s", "; ".join(problemen) or "geen 3D-model voor dit pand")
        return {**basis, "kader": kader, "foto": foto, "foto_reserve": reserve, "vlakken": varianten[0], "varianten": varianten,
                "schatting": True, "bron": "plattegrond", "problemen": problemen,
                "melding": ("Het 3D-model van je dak was niet bereikbaar of bestaat nog niet voor dit huis. Daarom zijn de dakvlakken geschat uit de "
                            f"plattegrond van je huis, met een helling van {AANGENOMEN_HELLING}°. Kijk op de luchtfoto of de nok klopt; "
                            "zo niet, kies 'Nok andersom'. Liggen er ook panelen op een plat deel (bijvoorbeeld een aanbouw), voeg dat dan "
                            "toe met 'Dakvlak erbij'." + (f" (Details: {'; '.join(problemen)})" if problemen else ""))}

    # 4. Niets bruikbaars: alleen de luchtfoto
    kader = [x - 20, y - 20, x + 20, y + 20]
    foto, reserve = _foto(kader)
    return {**basis, "kader": kader, "foto": foto, "foto_reserve": reserve, "vlakken": [], "handmatig": True, "problemen": problemen,
            "melding": "De dakgegevens waren niet bereikbaar, dus de dakvlakken zijn niet automatisch bepaald. Kijk op de luchtfoto hoe je "
                       "panelen liggen en vul ze hieronder zelf in, of probeer het later opnieuw."
                       + (f" (Details: {'; '.join(problemen)})" if problemen else "")}

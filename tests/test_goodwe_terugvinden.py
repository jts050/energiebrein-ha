"""De GoodWe zit niet meer op het ingestelde IP-adres: het brein zoekt hem en gebruikt het nieuwe adres."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'energiebrein/app'))
import goodwe
from brein.apparaten import echt


class _Inv:
    model_name, serial_number = "GW8K-ET", "123"


def _patch(monkeypatch, zoek, bereikbaar):
    """bereikbaar: de IP-adressen waar goodwe.connect lukt. zoek: wat de broadcast terugmeldt."""
    pogingen = []

    async def connect(host, port=8899, **kw):
        pogingen.append((host, port))
        if host in bereikbaar:
            return _Inv()
        raise OSError("geen antwoord")

    async def search():
        if isinstance(zoek, Exception):
            raise zoek
        return zoek

    async def dicht(*a, **k):
        return False
    monkeypatch.setattr(goodwe, "connect", connect)
    monkeypatch.setattr(goodwe, "search_inverters", search)
    monkeypatch.setattr(echt, "_poort_open", dicht)
    echt.GEVONDEN.clear()
    echt.BEKENDE_SERIENUMMERS.clear()
    return pogingen


def test_gewoon_verbinden_zoekt_niets(monkeypatch):
    p = _patch(monkeypatch, b"192.168.2.99,AABB,Solar-WiFi", {"192.168.2.5"})
    inv = asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert inv.model_name == "GW8K-ET" and echt.GEVONDEN == {}
    assert all(h == "192.168.2.5" for h, _ in p)


def test_nieuw_adres_wordt_gevonden(monkeypatch):
    _patch(monkeypatch, b"192.168.2.77,AABBCC,Solar-WiFi", {"192.168.2.77"})
    echt.BEKENDE_SERIENUMMERS["192.168.2.5"] = "123"
    inv = asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert inv.serial_number == "123"
    assert echt.GEVONDEN == {"192.168.2.5": "192.168.2.77"}


def test_niets_gevonden_geeft_de_gewone_melding(monkeypatch):
    _patch(monkeypatch, b"", set())
    with pytest.raises(echt.GoodWeNietBereikbaar) as e:
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert "192.168.2.5" in str(e.value) and echt.GEVONDEN == {}


def test_zoeken_mislukt_is_geen_extra_fout(monkeypatch):
    _patch(monkeypatch, OSError("geen netwerk"), set())
    with pytest.raises(echt.GoodWeNietBereikbaar):
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))


def test_gevonden_maar_niet_bereikbaar(monkeypatch):
    _patch(monkeypatch, b"192.168.2.88,AA,Solar-WiFi", set())
    with pytest.raises(echt.GoodWeNietBereikbaar) as e:
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert "192.168.2.88" in str(e.value)


def test_hetzelfde_adres_wordt_niet_nog_eens_geprobeerd(monkeypatch):
    p = _patch(monkeypatch, b"192.168.2.5,AA,Solar-WiFi", set())
    with pytest.raises(echt.GoodWeNietBereikbaar):
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert [h for h, _ in p].count("192.168.2.5") == len(p)        # alleen het ingestelde adres, geen tweede ronde


def test_start_zet_het_in_het_logboek_en_de_testknop_meldt_het(monkeypatch):
    class Log:
        regels = []
        def schrijf(self, soort, tekst, app=None):
            self.regels.append(tekst)

    _patch(monkeypatch, b"192.168.2.77,AA,Solar-WiFi", {"192.168.2.77"})

    echt.BEKENDE_SERIENUMMERS["192.168.2.5"] = "123"
    class Inv(_Inv):
        async def get_ems_mode(self):
            return None
    monkeypatch.setattr(goodwe, "connect", lambda host, port=8899, **kw: _ok(host))

    async def _ok(host):
        if host == "192.168.2.77":
            return Inv()
        raise OSError("nee")
    g = echt.GoodWe({"omvormer": {"host": "192.168.2.5"}, "accu": {}})
    g.logboek = Log()
    asyncio.run(g.start())
    assert any("192.168.2.77" in r and "vast IP" in r for r in g.logboek.regels)


def test_onbekend_serienummer_verbindt_niet_automatisch(monkeypatch):
    p = _patch(monkeypatch, b"192.168.2.77,AA,Solar-WiFi", {"192.168.2.77"})
    with pytest.raises(echt.GoodWeNietBereikbaar) as exc:
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert "192.168.2.77" in str(exc.value)
    assert all(host != "192.168.2.77" for host, _ in p)


def test_ander_serienummer_wordt_niet_gebruikt(monkeypatch):
    _patch(monkeypatch, b"192.168.2.77,AA,Solar-WiFi", {"192.168.2.77"})
    echt.BEKENDE_SERIENUMMERS["192.168.2.5"] = "andere-omvormer"
    with pytest.raises(echt.GoodWeNietBereikbaar, match="serienummer"):
        asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert not echt.GEVONDEN


def test_direct_verbonden_verwijdert_verouderd_zoekresultaat(monkeypatch):
    _patch(monkeypatch, b"", {"192.168.2.5"})
    echt.GEVONDEN["192.168.2.5"] = "192.168.2.77"
    asyncio.run(echt.goodwe_verbind("192.168.2.5"))
    assert not echt.GEVONDEN
    assert echt.BEKENDE_SERIENUMMERS["192.168.2.5"] == "123"

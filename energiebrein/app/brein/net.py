"""Eén manier om met internet te praten. Alleen IPv4 (op Windows blijft IPv6 soms hangen), proxy-instellingen
van de computer worden gevolgd, en een time-out geeft een begrijpelijke melding in plaats van een lege fout."""
from __future__ import annotations
import asyncio
import socket

import aiohttp


def sessie(totaal: float = 30, verbinden: float = 15, ipv4: bool = True) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(family=socket.AF_INET if ipv4 else 0), trust_env=True,
                                 timeout=aiohttp.ClientTimeout(total=totaal, connect=verbinden))


def uitleg(e: Exception) -> str:
    """Zet een netwerkfout om in gewone taal."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "geen antwoord binnen de wachttijd; mogelijk blokkeert een firewall, virusscanner of proxy de verbinding"
    if isinstance(e, aiohttp.ClientConnectorError):
        return f"kan geen verbinding maken ({e.os_error or e})"
    if isinstance(e, aiohttp.ClientResponseError):
        return f"de dienst antwoordde met foutcode {e.status}"
    return str(e) or e.__class__.__name__

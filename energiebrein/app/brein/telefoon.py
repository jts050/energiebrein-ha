"""Meldingen op je telefoon via de Home Assistant-app.

Draait Energiebrein als add-on in Home Assistant, dan geeft Home Assistant het een sleutel (SUPERVISOR_TOKEN).
Daarmee kan het brein de notify-dienst van je telefoon aanroepen, bijvoorbeeld notify.mobile_app_iphone_van_jordy.
Buiten Home Assistant (op de pc) is dit er niet en doet deze module niets."""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger("telefoon")
HA = "http://supervisor/core/api"


def beschikbaar() -> bool:
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def _kop() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}", "Content-Type": "application/json"}


async def diensten() -> list[str]:
    """Alle notify-diensten van Home Assistant, telefoons (mobile_app_…) eerst."""
    if not beschikbaar():
        return []
    from .net import sessie
    async with sessie(10) as s:
        async with s.get(f"{HA}/services", headers=_kop()) as r:
            r.raise_for_status()
            alles = await r.json(content_type=None)
    namen = []
    for d in alles:
        if d.get("domain") == "notify":
            namen += [n for n in (d.get("services") or {}) if n not in ("notify", "send_message", "persistent_notification")]
    return sorted(namen, key=lambda n: (not n.startswith("mobile_app_"), n))


async def stuur(dienst: str, titel: str, tekst: str, belangrijk: bool = False, acties: list | None = None):
    if not beschikbaar() or not dienst:
        raise RuntimeError("Telefoonmeldingen werken alleen als Energiebrein in Home Assistant draait.")
    from .net import sessie
    slug = os.environ.get("HOSTNAME", "local-energiebrein").replace("-", "_")
    pad = f"/hassio/ingress/{slug}"
    data = {"title": titel, "message": tekst,
            "data": {"tag": "energiebrein", "group": "energiebrein", "url": pad, "clickAction": pad}}
    if acties:
        # knoppen onder de melding (Home Assistant-app op iPhone en Android); het antwoord komt terug als
        # gebeurtenis mobile_app_notification_action, zie luister()
        data["data"]["actions"] = [{"action": a, "title": t} for a, t in acties]
        data["data"]["tag"] = "energiebrein-auto"
    if belangrijk:
        data["data"]["push"] = {"interruption-level": "time-sensitive"}
        data["data"]["priority"] = "high"
    async with sessie(15) as s:
        async with s.post(f"{HA}/services/notify/{dienst}", headers=_kop(), json=data) as r:
            if r.status == 400:
                raise RuntimeError(f"Home Assistant kent de dienst notify.{dienst} niet.")
            r.raise_for_status()


async def luister(bij_actie):
    """Luistert via de websocket van Home Assistant naar knoppen die op een melding zijn ingedrukt. Blijft draaien en
    maakt opnieuw verbinding als die wegvalt."""
    if not beschikbaar():
        return
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect("http://supervisor/core/websocket", heartbeat=30) as ws:
                    await ws.receive_json()                                   # auth_required
                    await ws.send_json({"type": "auth", "access_token": os.environ.get("SUPERVISOR_TOKEN", "")})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant weigert de websocket")
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"})
                    async for bericht in ws:
                        if bericht.type != aiohttp.WSMsgType.TEXT:
                            continue
                        d = bericht.json()
                        actie = ((d.get("event") or {}).get("data") or {}).get("action")
                        if d.get("type") == "event" and actie and str(actie).startswith("EB_"):
                            try:
                                await bij_actie(actie)
                            except Exception:
                                log.exception("Knop %s verwerken mislukt", actie)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Luisteren naar knoppen op meldingen mislukt (%s); over een minuut opnieuw", e)
        await asyncio.sleep(60)


class Telefoon:
    """Stuurt een melding bij een nieuwe storing en als die is opgelost. Niet vaker dan eens per kwartier per onderwerp."""

    def __init__(self, cfg: dict):
        m = cfg.get("meldingen") or {}
        self.dienst = (m.get("telefoon") or "").strip()
        self.bij_opgelost = m.get("bij_opgelost", True)
        self._laatst: dict[str, datetime] = {}

    @property
    def aan(self) -> bool:
        return beschikbaar() and bool(self.dienst)

    def meld(self, onderwerp: str, titel: str, tekst: str, belangrijk: bool = False, acties: list | None = None):
        if not self.aan:
            return
        nu = datetime.now()
        if onderwerp in self._laatst and nu - self._laatst[onderwerp] < timedelta(minutes=15):
            return
        self._laatst[onderwerp] = nu

        async def _doe():
            try:
                await stuur(self.dienst, titel, tekst, belangrijk, acties)
            except Exception as e:
                log.warning("Telefoonmelding mislukt: %s", e)
        try:
            asyncio.get_running_loop().create_task(_doe())
        except RuntimeError:
            pass

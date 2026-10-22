"""Pincode voor wie Energiebrein rechtstreeks opent (bijvoorbeeld via Tailscale op je telefoon).

Via Home Assistant (ingress) is er geen pincode nodig: daar heb je al ingelogd bij Home Assistant.
Zonder ingestelde pincode werkt alles zoals altijd."""
from __future__ import annotations
import hashlib
import hmac
import secrets
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import DATA

KOEKJE = "eb_toegang"
VRIJ = {"/manifest.json", "/icoon-192.png", "/icoon-512.png", "/api/aanmelden"}
HA_SUPERVISOR = "172.30.32.2"   # vanaf dit adres komt ingress van Home Assistant


def _sleutel() -> bytes:
    pad = Path(DATA) / "sessie.key"
    try:
        return bytes.fromhex(pad.read_text().strip())
    except Exception:
        pad.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_hex(32)
        pad.write_text(s)
        return bytes.fromhex(s)


def _teken(pin: str) -> str:
    return hmac.new(_sleutel(), b"eb|" + hashlib.sha256(pin.encode()).digest(), hashlib.sha256).hexdigest()


def via_home_assistant(req: Request) -> bool:
    return bool(req.headers.get("x-ingress-path")) and (req.client and req.client.host == HA_SUPERVISOR)


class Toegang:
    def __init__(self, cfg_ophalen):
        self.cfg = cfg_ophalen
        self.pogingen: dict[str, list[float]] = {}

    def pin(self) -> str:
        return str(((self.cfg() or {}).get("toegang") or {}).get("pincode") or "").strip()

    def binnen(self, req: Request) -> bool:
        pin = self.pin()
        if not pin or via_home_assistant(req):
            return True
        k = req.cookies.get(KOEKJE, "")
        return bool(k) and hmac.compare_digest(k, _teken(pin))

    def te_vaak(self, ip: str) -> bool:
        nu = time.time()
        lijst = [t for t in self.pogingen.get(ip, []) if nu - t < 300]
        self.pogingen[ip] = lijst
        return len(lijst) >= 5

    async def middleware(self, req: Request, volgende):
        pad = req.url.path
        if pad in VRIJ or self.binnen(req):
            return await volgende(req)
        if pad.startswith("/api/"):
            return JSONResponse({"detail": "Vul eerst je pincode in."}, status_code=401)
        return HTMLResponse(LOGIN, headers={"Cache-Control": "no-store"})

    async def aanmelden(self, req: Request):
        ip = req.client.host if req.client else "?"
        if self.te_vaak(ip):
            return JSONResponse({"detail": "Te vaak geprobeerd. Wacht 5 minuten."}, status_code=429)
        try:
            pin = str((await req.json()).get("pin") or "").strip()
        except Exception:
            pin = ""
        echt = self.pin()
        if not echt or hmac.compare_digest(pin, echt):
            r = JSONResponse({"ok": True})
            if echt:
                r.set_cookie(KOEKJE, _teken(echt), max_age=180 * 86400, httponly=True, samesite="lax")
            return r
        self.pogingen.setdefault(ip, []).append(time.time())
        return JSONResponse({"detail": "Pincode klopt niet."}, status_code=403)


LOGIN = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0F1419"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icoon-192.png">
<title>Energiebrein</title>
<style>
:root{--bg:#0F1419;--ink:#E8ECEF;--muted:#8A96A0;--line:#222C34;--surface:#151C22;--accu:#6FCF97;--bad:#EB6F6F}
@media (prefers-color-scheme:light){:root{--bg:#F6F7F8;--ink:#12181D;--muted:#5D6973;--line:#E1E5E8;--surface:#fff;--accu:#1F9D61;--bad:#D14343}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:400 16px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;place-items:center;padding:24px}
main{width:100%;max-width:320px;text-align:center}
.logo{width:56px;height:56px;margin:0 auto 18px}
h1{font-size:22px;font-weight:600;margin:0 0 4px;letter-spacing:-.01em}
p{color:var(--muted);margin:0 0 24px;font-size:14px}
input{width:100%;font:600 28px/1 inherit;letter-spacing:.5em;text-align:center;padding:14px 10px 14px 22px;border-radius:12px;border:1px solid var(--line);background:var(--surface);color:var(--ink)}
input:focus{outline:2px solid var(--accu);outline-offset:2px}
button{margin-top:14px;width:100%;padding:13px;border:0;border-radius:12px;background:var(--ink);color:var(--bg);font:600 16px inherit;cursor:pointer}
.fout{color:var(--bad);font-size:14px;min-height:22px;margin-top:12px}
</style></head><body><main>
<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="10" fill="#6FCF97"/><path d="M9 17 16 9l7 8M11 15.5V23h10v-7.5" fill="none" stroke="#0F1419" stroke-width="2.4" stroke-linejoin="round"/></svg>
<h1>Energiebrein</h1><p>Vul je pincode in</p>
<form id="f"><input id="pin" type="password" inputmode="numeric" autocomplete="current-password" maxlength="12" autofocus aria-label="Pincode">
<button type="submit">Openen</button><div class="fout" id="fout"></div></form>
<script>
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const f=document.getElementById('fout');f.textContent='';
 try{const r=await fetch('api/aanmelden',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:document.getElementById('pin').value})});
  if(r.ok){location.reload();return;}const d=await r.json().catch(()=>({}));f.textContent=d.detail||'Lukt niet';}
 catch(err){f.textContent='Geen verbinding met Energiebrein';}};
</script></main></body></html>"""

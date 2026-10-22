"""Start het Energiebrein:  python start.py   (of: python start.py --config mijn.yaml --open)"""
import argparse
import logging
import socket
import threading
import webbrowser

import uvicorn

from brein.config import laad
from brein.web import maak_app

log = logging.getLogger("start")


def vrije_poort(voorkeur: int) -> int:
    """Gebruik de ingestelde poort; is die bezet door een ander programma, neem dan de eerstvolgende vrije."""
    for poort in range(voorkeur, voorkeur + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", poort))
                return poort
            except OSError:
                log.warning("Poort %s is bezet, ik probeer %s", poort, poort + 1)
    raise SystemExit(f"Geen vrije poort gevonden tussen {voorkeur} en {voorkeur + 19}.")


def eigen_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 80))   # er wordt niets verstuurd; dit geeft alleen het eigen netwerkadres
            return s.getsockname()[0]
    except OSError:
        return "localhost"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Energiebrein — slim energiemanagement voor thuis")
    p.add_argument("--config", help="pad naar config.yaml")
    p.add_argument("--poort", type=int, help="vaste poort voor de app (anders die uit config.yaml)")
    p.add_argument("--open", action="store_true", help="open de app in de browser zodra hij klaar is")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-12s %(message)s", datefmt="%H:%M:%S")
    cfg = laad(args.config)
    if cfg.get("simulatie"):
        log.info("SIMULATIE: er worden geen echte apparaten aangestuurd")
    poort = args.poort or vrije_poort(cfg["web"]["poort"])

    def klaar():
        # wordt aangeroepen nadat het brein volledig is opgestart
        url = f"http://localhost:{poort}"
        print("\n" + "=" * 60, flush=True)
        print(f"  Energiebrein is KLAAR.  Open:  {url}", flush=True)
        print(f"  Op je telefoon of iPad (zelfde wifi):  http://{eigen_ip()}:{poort}", flush=True)
        print("=" * 60 + "\n", flush=True)
        if args.open:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    app = maak_app(cfg, na_start=klaar, config_pad=args.config)
    uvicorn.run(app, host="0.0.0.0", port=poort, log_level="warning")

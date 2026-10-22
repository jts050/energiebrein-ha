"""Leest config.yaml en vult ontbrekende waarden aan met standaarden."""
from __future__ import annotations
import copy
import os
from pathlib import Path
import yaml

BASIS = Path(__file__).resolve().parent.parent
# Waar metingen, AI-gegevens en de licentie bewaard worden. In de Home Assistant-add-on is dat /data,
# zodat alles een update of herstart overleeft.
DATA = Path(os.environ.get("ENERGIEBREIN_DATA") or (BASIS / "data"))
VOORBEELD = BASIS / "config.example.yaml"


def data_map(cfg: dict) -> Path:
    """Simulatie en je echte huis hebben elk hun eigen geschiedenis, zodat nagebootste data nooit in het echte leren terechtkomt."""
    d = DATA / ("simulatie" if cfg.get("simulatie", True) else "thuis")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def laad(pad: str | Path | None = None) -> dict:
    standaard = yaml.safe_load(VOORBEELD.read_text(encoding="utf-8"))
    pad = Path(pad) if pad else BASIS / "config.yaml"
    cfg = _merge(standaard, yaml.safe_load(pad.read_text(encoding="utf-8")) or {}) if pad.exists() else standaard
    # oude standaardwaarde van de energiebelasting (een schatting voor 2027) vervangen door het echte tarief van 2026
    if abs(float(cfg.get("prijzen", {}).get("energiebelasting", 0)) - 0.1065) < 1e-9:
        cfg["prijzen"]["energiebelasting"] = standaard["prijzen"]["energiebelasting"]
    return cfg

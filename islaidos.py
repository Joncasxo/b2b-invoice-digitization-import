"""
AI ISLAIDU ZURNALAS — kiek centu kuris vartotojas isnaudojo (rodoma /admin).

Irasoma po KIEKVIENOS AI ekstrakcijos: vardas = darbuotojo folderis, kuriame
gulejo saskaita (rankiniam ikelimui be folderio — "Bendra"). Saugoma pagal
menesius, kad matytusi ir einamasis menuo, ir visa istorija.
Formatas: {"Petras": {"2026-08": 12.34, ...}, ...}  (centai)
"""

import json
import os
import threading
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
FAILAS = os.path.join(_DIR, "islaidos.json")

# Fone saskaitos apdorojamos KELIOS VIENU METU, o cia skaitom-pakeiciam-irasom.
# Be uzrakto du srautai perrasytu vienas kito suma (islaidos dingtu).
_UZRAKTAS = threading.Lock()


def _skaityti() -> dict:
    if not os.path.exists(FAILAS):
        return {}
    try:
        with open(FAILAS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _rasyti(d: dict) -> None:
    """Irasom per laikina faila: skaitantis niekada nepagauna pusiau irasyto JSON."""
    laik = FAILAS + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(laik, FAILAS)


def prideti(vardas: str, ct) -> None:
    """Prideda israsyta AI kaina (centais) vartotojui uz einamaji menesi."""
    try:
        ct = float(ct or 0)
    except (TypeError, ValueError):
        return
    if ct <= 0:
        return
    vardas = (vardas or "").strip() or "Bendra"
    menuo = datetime.now().strftime("%Y-%m")
    with _UZRAKTAS:
        d = _skaityti()
        v = d.setdefault(vardas, {})
        v[menuo] = round(v.get(menuo, 0) + ct, 2)
        _rasyti(d)


def suvestine() -> list[dict]:
    """[{vardas, sis_menuo, viso, pagal_menesius}] — didziausios islaidos virsuje."""
    menuo = datetime.now().strftime("%Y-%m")
    out = []
    for vardas, men in _skaityti().items():
        out.append({
            "vardas": vardas,
            "sis_menuo": round(men.get(menuo, 0), 2),
            "viso": round(sum(men.values()), 2),
            "pagal_menesius": men,
        })
    out.sort(key=lambda x: -x["viso"])
    return out

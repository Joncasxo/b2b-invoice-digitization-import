"""
AGENTU ZINUTES — dvipusis kanalas tarp debesies ir Pragmos serverio agentu
PER MUSU SERVERI (Pragmos agento pasiulymas 2026-09-05: jo Claude aplinkoje
nera SendMessage irankio, o musu serveriu jo agentas ir taip kalbasi kas cikla).

POST /api/agentas/zinutes            {"kam": "pragma"|"debesis", "tekstas": "...", "nuo": "..."}
GET  /api/agentas/zinutes?kam=pragma  -> atiduoda NEPAIMTAS zinutes ir pazymi paimtomis.

Istorija lieka faile pragma_zinutes.json — niekas netrinama, "paimta" tik
uzsipildo laiku. Auth — tas pats X-Agento-Raktas (middleware).
"""

import json
import os
import threading
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
_FAILAS = os.path.join(_DIR, "pragma_zinutes.json")
_UZRAKTAS = threading.Lock()

GAVEJAI = ("pragma", "debesis")


def _skaityti() -> list:
    try:
        with open(_FAILAS, encoding="utf-8") as f:
            return json.load(f).get("zinutes") or []
    except (OSError, json.JSONDecodeError):
        return []


def _irasyti(zinutes: list) -> None:
    laik = _FAILAS + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump({"zinutes": zinutes}, f, ensure_ascii=False, indent=1)
    os.replace(laik, _FAILAS)


def palikti(kam: str, tekstas: str, nuo: str = "") -> dict:
    """Palieka zinute gavejui ('pragma' arba 'debesis'). Grazina irasa."""
    kam = (kam or "").strip().lower()
    tekstas = str(tekstas or "").strip()
    if kam not in GAVEJAI:
        raise ValueError("Laukas 'kam' turi buti 'pragma' arba 'debesis'")
    if not tekstas:
        raise ValueError("Tuscias tekstas")
    with _UZRAKTAS:
        zinutes = _skaityti()
        z = {
            "id": (zinutes[-1]["id"] + 1) if zinutes else 1,
            "kam": kam,
            "nuo": str(nuo or "").strip()[:80],
            "tekstas": tekstas[:20000],
            "laikas": datetime.now().isoformat(timespec="seconds"),
            "paimta": None,
        }
        zinutes.append(z)
        _irasyti(zinutes)
        return z


def paimti(kam: str) -> list:
    """Grazina gavejo NEPAIMTAS zinutes ir pazymi jas paimtomis."""
    kam = (kam or "").strip().lower()
    if kam not in GAVEJAI:
        raise ValueError("Laukas 'kam' turi buti 'pragma' arba 'debesis'")
    with _UZRAKTAS:
        zinutes = _skaityti()
        naujos = [z for z in zinutes if z.get("kam") == kam and not z.get("paimta")]
        if naujos:
            dabar = datetime.now().isoformat(timespec="seconds")
            for z in naujos:
                z["paimta"] = dabar
            _irasyti(zinutes)
        return [{"id": z["id"], "nuo": z.get("nuo") or "", "tekstas": z["tekstas"],
                 "laikas": z["laikas"]} for z in naujos]


def visos(kam: str = "", kiek: int = 100) -> list:
    """Istorija perziurai (su 'paimta' zymomis) — netrina nieko."""
    zinutes = _skaityti()
    if kam:
        zinutes = [z for z in zinutes if z.get("kam") == kam]
    return zinutes[-kiek:]

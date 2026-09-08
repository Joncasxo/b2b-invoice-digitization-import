"""
IMPORTO REZULTATAI — galutine kiekvienos i Pragma issiustos saskaitos busena:
ikelta (+pragmos_id) / dublikatas (+sutapo_su_id) / klaida (+priezastis).

Agentas siuncia po VIENA saskaita IS KARTO po ikelimo bandymo; jo puseje
nepavyke pranesimai kartojami (_nepranesta eile), tad tyla reiskia
"dar nezinoma", ne "nepavyko". Zurnalas — istorija, nesitrina.
"""

import json
import os
import threading
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
_FAILAS = os.path.join(_DIR, "pragma_importo_rezultatai.json")
_UZRAKTAS = threading.Lock()


def _skaityti() -> list:
    try:
        with open(_FAILAS, encoding="utf-8") as f:
            return json.load(f).get("rezultatai") or []
    except (OSError, json.JSONDecodeError):
        return []


def registruoti(rez: dict) -> dict:
    """Prideda rezultata i zurnala; grazina issaugota irasa."""
    irasas = {
        "failas": str(rez.get("failas") or ""),
        "duombaze": str(rez.get("duombaze") or ""),
        "numeris": str(rez.get("numeris") or ""),
        "data": str(rez.get("data") or ""),
        "tiekejas": str(rez.get("tiekejas") or ""),
        "imones_kodas": str(rez.get("imones_kodas") or ""),
        "busena": str(rez.get("busena") or ""),
        "priezastis": str(rez.get("priezastis") or ""),
        "pragmos_id": rez.get("pragmos_id"),
        "sutapo_su_id": rez.get("sutapo_su_id"),
        "dokumentas": str(rez.get("dokumentas") or "pirkimas"),
        "pardavimo_numeris": str(rez.get("pardavimo_numeris") or ""),
        "pirkejas": str(rez.get("pirkejas") or ""),
        "gauta": datetime.now().isoformat(timespec="seconds"),
    }
    with _UZRAKTAS:
        rezultatai = _skaityti()
        rezultatai.append(irasas)
        laik = _FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump({"rezultatai": rezultatai}, f, ensure_ascii=False, indent=1)
        os.replace(laik, _FAILAS)
    return irasas


def paskutiniai(kiek: int = 100) -> list:
    return _skaityti()[-kiek:]

"""
Sugeneruotu XML eksportas Pragmos serverio agentui.

Kiekvienas "Generuoti XML" paspaudimas papildomai issaugo faila i eksportas/
ir iraso i zurnala (eksportas/zurnalas.json) su pozymiu "paimta".
Agentas Pragmos serveryje per HTTPS:
  1) pasiima NEPAIMTU sarasa,
  2) parsisiuncia failus,
  3) pazymi juos paimtais (kad kita karta nebegautu).

Failai niekada netrinami — jei reikia, ta pati faila galima parsisiusti dar karta
(sarasas su ?visi=1 rodo ir paimtus).
"""

import json
import os
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
EKSPORTO_DIR = os.path.join(_DIR, "eksportas")
_ZURNALAS = os.path.join(EKSPORTO_DIR, "zurnalas.json")


def _skaityti() -> list[dict]:
    if not os.path.exists(_ZURNALAS):
        return []
    try:
        with open(_ZURNALAS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _rasyti(irasai: list[dict]) -> None:
    os.makedirs(EKSPORTO_DIR, exist_ok=True)
    with open(_ZURNALAS, "w", encoding="utf-8") as f:
        json.dump(irasai, f, ensure_ascii=False, indent=1)


def issaugoti(saskaitos_id: str, failo_vardas: str, turinys: str, darbuotojas: str = "") -> dict:
    """Padeda XML i eksportas/ ir pazymi kaip NEPAIMTA (net jei generuota pakartotinai).
    `darbuotojas` — i kurio darbuotojo importo folderi agentas turi padeti faila."""
    failo_vardas = os.path.basename(failo_vardas)
    os.makedirs(EKSPORTO_DIR, exist_ok=True)
    # utf-8-sig = UTF-8 su BOM, kaip reikalauja Pragmos importas
    with open(os.path.join(EKSPORTO_DIR, failo_vardas), "w", encoding="utf-8-sig") as f:
        f.write(turinys)

    irasai = _skaityti()
    for ir in irasai:
        if ir["failas"] == failo_vardas:
            ir.update({"saskaitos_id": saskaitos_id, "sukurta": time.time(),
                       "paimta": False, "darbuotojas": darbuotojas})
            _rasyti(irasai)
            return ir
    ir = {"failas": failo_vardas, "saskaitos_id": saskaitos_id, "sukurta": time.time(),
          "paimta": False, "darbuotojas": darbuotojas}
    irasai.append(ir)
    _rasyti(irasai)
    return ir


def sarasas(visi: bool = False) -> list[dict]:
    irasai = sorted(_skaityti(), key=lambda x: -x.get("sukurta", 0))
    return irasai if visi else [ir for ir in irasai if not ir.get("paimta")]


def kelias(failo_vardas: str) -> str | None:
    k = os.path.join(EKSPORTO_DIR, os.path.basename(failo_vardas))
    return k if os.path.exists(k) else None


def pazymeti_paimta(failo_vardas: str) -> bool:
    failo_vardas = os.path.basename(failo_vardas)
    irasai = _skaityti()
    for ir in irasai:
        if ir["failas"] == failo_vardas:
            ir["paimta"] = True
            ir["paimta_laikas"] = time.time()
            _rasyti(irasai)
            return True
    return False

"""
ATMINTIS — ismoksta ZMOGAUS parinkimus (tiekejo preke -> pr kortele) ir kita karta
uzdeda automatiskai. 0 tokenu, grynas kodas, saugoma atmintis.json faile.

Kada issaugoma: spaudziant „Generuoti XML" (tai patvirtinimo momentas — zmogus jau
patikrino). Kiekvienai eilutei su parinktu pr kodu upsert'inamas irasas.

Kaip atpazistama kita karta (is eiles):
  1) tiekejas: pagal imones koda (jei abu turi), kitaip pagal normalizuota pavadinima
  2) preke:    pagal tiekejo prekes koda -> pagal EAN -> pagal normalizuota pavadinima
"""

import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import date

# Atminti raso ir XML generavimas (vartotojo veiksmas), ir Pragmos importo
# pranesimas (agento POST) — abu daro skaityk-pakeisk-irasyk. Be uzrakto du
# vienalaikiai irasymai perrasytu vienas kito pakeitimus.
_UZRAKTAS = threading.Lock()

# Atmintis — KIEKVIENOS APLINKOS sava (zr. aplinka.py): kiekviena funkcija gauna
# tos paskyros atmintis.json kelia. Vienos apskaitininkes ismokti priskyrimai
# nepatenka i kitos programa.


def _be_diakritikos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def _norm_pav(t) -> str:
    """Pavadinimo normalizavimas: mazosios, be diakritikos, tik raides/skaitmenys."""
    t = _be_diakritikos(str(t or "").lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _norm_kodas(t) -> str:
    """Kodo normalizavimas: be tarpu/skyrybos (0715 234 090 == 0715234090)."""
    return re.sub(r"\W", "", str(t or "")).lower()


def _uzkrauti(failas: str) -> dict:
    if not os.path.exists(failas):
        return {"irasai": []}
    try:
        with open(failas, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("irasai"), list):
            return d
    except Exception:
        pass
    return {"irasai": []}


def _irasyti(failas: str, d: dict) -> None:
    os.makedirs(os.path.dirname(failas), exist_ok=True)
    # Per laikina faila — skaitantis niekada nepagauna pusiau irasyto JSON
    laik = failas + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(laik, failas)


def _tiekejas_sutampa(irasas: dict, pavadinimas: str, imones_kodas: str) -> bool:
    ik = _norm_kodas(imones_kodas)
    if ik and irasas.get("tiekejo_imones_kodas"):
        return _norm_kodas(irasas["tiekejo_imones_kodas"]) == ik
    return irasas.get("tiekejas_norm") == _norm_pav(pavadinimas)


def _preke_sutampa(irasas: dict, eilute: dict) -> bool:
    tk = _norm_kodas(eilute.get("tiekejo_kodas"))
    if tk and irasas.get("tiekejo_prekes_kodas_norm"):
        return irasas["tiekejo_prekes_kodas_norm"] == tk
    ean = _norm_kodas(eilute.get("ean"))
    if ean and irasas.get("ean_norm"):
        return irasas["ean_norm"] == ean
    return bool(irasas.get("preke_norm")) and irasas["preke_norm"] == _norm_pav(eilute.get("pavadinimas"))


def rasti_visiems(failas: str, tiekejas: dict, eilutes: list[dict]) -> list[dict | None]:
    """Kiekvienai eilutei — atminties irasas arba None."""
    d = _uzkrauti(failas)
    pav = tiekejas.get("pavadinimas") or ""
    ik = tiekejas.get("imones_kodas") or ""
    tinkami = [i for i in d["irasai"] if _tiekejas_sutampa(i, pav, ik)]
    out: list[dict | None] = []
    for e in eilutes:
        rasta = next((i for i in tinkami if _preke_sutampa(i, e)), None)
        if rasta:
            out.append({
                "pr_kodas": rasta["pr_kodas"],
                "pr_pavadinimas": rasta.get("pr_pavadinimas") or "",
                "savikaina": rasta.get("savikaina"),
                "vienetas": rasta.get("vienetas") or "",
                "kartai": rasta.get("kartai") or 1,
            })
        else:
            out.append(None)
    return out


def issaugoti_is_xml(failas: str, duomenys: dict) -> int:
    """Po „Generuoti XML" — isimena kiekviena eilute su parinktu pr kodu. Grazina kiek irasu."""
    tie = duomenys.get("tiekejas") or {}
    pav = tie.get("pavadinimas") or ""
    ik = tie.get("imones_kodas") or ""
    if not pav and not ik:
        return 0

    with _UZRAKTAS:
        return _issaugoti_is_xml_uzrakinta(failas, duomenys, pav, ik)


def _issaugoti_is_xml_uzrakinta(failas: str, duomenys: dict, pav: str, ik: str) -> int:
    d = _uzkrauti(failas)
    kiek = 0
    for e in duomenys.get("eilutes") or []:
        pr = (e.get("pirkejo_kodas") or "").strip()
        if not pr:
            continue  # neparinkta — nera ko iseiti isiminti
        esamas = next(
            (i for i in d["irasai"] if _tiekejas_sutampa(i, pav, ik) and _preke_sutampa(i, e)),
            None,
        )
        savikaina = e.get("vnt_kaina")
        if esamas:
            esamas.update({
                "pr_kodas": pr,
                "pr_pavadinimas": e.get("pirkejo_pavadinimas") or esamas.get("pr_pavadinimas") or "",
                "savikaina": savikaina if savikaina is not None else esamas.get("savikaina"),
                "vienetas": e.get("vienetas") or esamas.get("vienetas") or "",
                "kartai": (esamas.get("kartai") or 1) + 1,
                "atnaujinta": date.today().isoformat(),
            })
        else:
            d["irasai"].append({
                "id": uuid.uuid4().hex[:10],
                "tiekejas": pav,
                "tiekejas_norm": _norm_pav(pav),
                "tiekejo_imones_kodas": ik,
                "preke": e.get("pavadinimas") or "",
                "preke_norm": _norm_pav(e.get("pavadinimas")),
                "tiekejo_prekes_kodas": e.get("tiekejo_kodas") or "",
                "tiekejo_prekes_kodas_norm": _norm_kodas(e.get("tiekejo_kodas")),
                "ean": e.get("ean") or "",
                "ean_norm": _norm_kodas(e.get("ean")),
                "pr_kodas": pr,
                "pr_pavadinimas": e.get("pirkejo_pavadinimas") or "",
                "savikaina": savikaina,
                "vienetas": e.get("vienetas") or "",
                "kartai": 1,
                "atnaujinta": date.today().isoformat(),
            })
        kiek += 1
    if kiek:
        _irasyti(failas, d)
    return kiek


def visi(failas: str) -> list[dict]:
    return _uzkrauti(failas)["irasai"]


def trinti(failas: str, irasas_id: str) -> bool:
    with _UZRAKTAS:
        d = _uzkrauti(failas)
        pries = len(d["irasai"])
        d["irasai"] = [i for i in d["irasai"] if i.get("id") != irasas_id]
        if len(d["irasai"]) != pries:
            _irasyti(failas, d)
            return True
        return False

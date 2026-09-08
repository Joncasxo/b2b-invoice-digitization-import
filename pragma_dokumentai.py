"""
PRAGMOS DOKUMENTU BUSENOS — gyvos Pragmos DB veidrodis (pirkimai + pardavimai).

Pragmos serverio agentas kas 5 min siuncia POST /api/agentas/dokumentai
paketais (~300 irasu). Mes UPSERT'inam ir atsakome {"priimta": N} — agentas
zymeklio nestumia, kol negauna skaiciaus (apsauga nuo tylaus duomenu praradimo:
tuscias 200 reikstu "gavom", nors nieko neissaugojom).

Kam naudojama: zymes saskaitu sarase — "Pragmoje" (dokumentas jau ju DB,
importuotas ARBA suvestas ranka) + ispejimas pries kartotini XML
(apsauga nuo dvigubo suvedimo).

Unikalumo raktas: imones_kodas|numeris|data — vien numeris per visa baze
nera unikalus (skirtingu tiekeju numeriai sutampa).
"""

import json
import os
import re
import threading

_DIR = os.path.dirname(os.path.abspath(__file__))
_FAILAS = os.path.join(_DIR, "pragma_dokumentai.json")
_UZRAKTAS = threading.Lock()

_saugykla: dict | None = None      # raktas "kodas|nr|data" -> dokumentas
_pirkimu_indeksas: dict = {}       # "kodas|nr" -> dokumentas (greita paieska sarasui)
# ATSARGINIS matchas vien SKAITMENIMIS: PDF sriftai gadina lietuviskas raides
# (pvz. serija su Į teksto sluoksnyje netenka raides), o skaitmenu negadina.
# "kodas|skaitmenys" -> dokumentas; False = pas ta tiekeja KELI skirtingi numeriai
# su ta pacia skaitmenu seka — dviprasmiska, tokiu NEmatchinam (jokiu klaidingu poru).
_pirkimu_skaitmenys: dict = {}
_MIN_SKAITMENU = 5


def _norm_nr(nr) -> str:
    """Kaip server._norm_numeris + didziosios: 'Serija KAA Nr. 36426' -> 'KAA36426'."""
    t = re.sub(r"(?i)\bnr\.?(?=[\s\d]|$)", "", str(nr or ""))
    return re.sub(r"[\s\-]+", "", t).upper()


def _norm_kodas(k) -> str:
    return re.sub(r"\D", "", str(k or ""))


def _indeksuoti(dok: dict) -> None:
    # Pardavimu numeriai (musu TKL/VDB) sarase nezymimi — indeksuojam tik pirkimus
    if dok.get("tipas") == "pardavimas":
        return
    ik, nr = dok.get("imones_kodas") or "", _norm_nr(dok.get("numeris"))
    if not ik or not nr:
        return
    _pirkimu_indeksas[f"{ik}|{nr}"] = dok
    sk = re.sub(r"\D", "", nr)
    if len(sk) >= _MIN_SKAITMENU:
        raktas = f"{ik}|{sk}"
        buves = _pirkimu_skaitmenys.get(raktas)
        if buves is False:
            return                       # jau zinoma kaip dviprasmiska
        if buves is not None and _norm_nr(buves.get("numeris")) != nr:
            _pirkimu_skaitmenys[raktas] = False   # kitas numeris ta pacia seka
        else:
            _pirkimu_skaitmenys[raktas] = dok


def _uzkrauti() -> dict:
    global _saugykla
    if _saugykla is None:
        with _UZRAKTAS:
            if _saugykla is None:
                try:
                    with open(_FAILAS, encoding="utf-8") as f:
                        _saugykla = json.load(f)
                except (OSError, json.JSONDecodeError):
                    _saugykla = {}
                for dok in _saugykla.values():
                    _indeksuoti(dok)
    return _saugykla


def _irasyti() -> None:
    laik = _FAILAS + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump(_saugykla, f, ensure_ascii=False)
    os.replace(laik, _FAILAS)


NUMATYTA_DUOMBAZE = ""   # srautas be `duombaze` lauko = tikroji baze (nustatymu duombaze; agentas siuncia tik ja)


def upsert(dokumentai: list[dict], duombaze: str = "") -> int:
    """Issaugo paketa; grazina KIEK irasu priimta (agentui — patvirtinimas).
    `duombaze` — paketo baze (agentas siuncia virsuje salia saltinis); saugoma
    prie kiekvieno dokumento, kad zyme „jau Pragmoje" zinotu, KURIOJE bazeje
    (i BANDYMAI keliama saskaita, kuri yra tikroje bazeje, — ne dublikatas)."""
    _uzkrauti()
    priimta = 0
    with _UZRAKTAS:
        for dok in dokumentai or []:
            if not isinstance(dok, dict):
                continue
            nr = _norm_nr(dok.get("numeris"))
            if not nr:
                continue
            ik = _norm_kodas(dok.get("imones_kodas"))
            data = str(dok.get("data") or "")[:10]
            irasas = {
                "tipas": str(dok.get("tipas") or ""),
                "numeris": str(dok.get("numeris") or "").strip(),
                "data": data,
                "imones_kodas": ik,
                "imone": str(dok.get("imone") or "").strip(),
                "busena": str(dok.get("busena") or "").strip(),
                "suma": dok.get("suma"),
                "pragmos_id": dok.get("pragmos_id"),
                "duombaze": str(dok.get("duombaze") or duombaze or NUMATYTA_DUOMBAZE).strip(),
            }
            _saugykla[f"{ik}|{nr}|{data}"] = irasas
            _indeksuoti(irasas)
            priimta += 1
        if priimta:
            _irasyti()
    return priimta


def rasti_pirkima(imones_kodas, numeris) -> dict | None:
    """Ar TIEKEJO saskaita (kodas + numeris) jau yra Pragmos DB. O(1).

    Jei tikslus numeris nepataiko — atsarginis lyginimas vien skaitmenimis
    (raides PDF sriftuose buna sugadintos/pamestos), bet TIK kai ta skaitmenu
    seka pas ta tiekeja vienintele."""
    ik, nr = _norm_kodas(imones_kodas), _norm_nr(numeris)
    if not ik or not nr:
        return None
    _uzkrauti()
    dok = _pirkimu_indeksas.get(f"{ik}|{nr}")
    if dok:
        return dok
    sk = re.sub(r"\D", "", nr)
    if len(sk) >= _MIN_SKAITMENU:
        return _pirkimu_skaitmenys.get(f"{ik}|{sk}") or None
    return None


def kiek() -> int:
    return len(_uzkrauti())

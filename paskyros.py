"""
Darbuotoju PASKYROS su individualiais slaptazodziais (admin panele /admin).

Veikimo taisykles:
  - Paskyros saugomos paskyros.json (slaptazodziai — PBKDF2 hash, ne tekstu).
  - Prisijungimas: jei VARDAS turi paskyra -> tikrinamas JOS slaptazodis;
    jei paskyros nera -> galioja bendras PRISIJUNGIMO_SLAPTAZODIS (kaip iki siol).
    Taip nieko nesulauzo: paskyras galima kurti palaipsniui.
  - Pirmoji sukurta paskyra automatiskai tampa ADMIN.
  - Kol paskyru nera ne vienos, /admin prieinamas visiems prisijungusiems
    (kad butu imanoma susikurti pirmaja paskyra).
  - Slaptazodziai generuojami patys (rodomi VIENA karta sukurus) — niekur netvarkomi
    atviru tekstu ir neatstatomi, tik pakeiciami nauju.
"""

import hashlib
import json
import os
import secrets
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
FAILAS = os.path.join(_DIR, "paskyros.json")

# Slaptazodis be panasiu simboliu (0/O, 1/l/I) — kad lengva perduoti zodziu
_ABC = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generuoti_slaptazodi(ilgis: int = 10) -> str:
    return "".join(secrets.choice(_ABC) for _ in range(ilgis))


def _hash(slaptazodis: str, druska: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", slaptazodis.encode("utf-8"),
                               bytes.fromhex(druska), 200_000).hex()


def _skaityti() -> list[dict]:
    if not os.path.exists(FAILAS):
        return []
    try:
        with open(FAILAS, encoding="utf-8") as f:
            return json.load(f).get("paskyros") or []
    except Exception:
        return []


def _rasyti(paskyros: list[dict]) -> None:
    with open(FAILAS, "w", encoding="utf-8") as f:
        json.dump({"paskyros": paskyros}, f, ensure_ascii=False, indent=1)


def _rasti(paskyros: list[dict], vardas: str) -> dict | None:
    v = (vardas or "").strip().lower()
    for p in paskyros:
        if p["vardas"].strip().lower() == v:
            return p
    return None


def yra_paskyru() -> bool:
    return len(_skaityti()) > 0


def turi_paskyra(vardas: str) -> bool:
    return _rasti(_skaityti(), vardas) is not None


def tikrinti(vardas: str, slaptazodis: str) -> bool:
    """True, jei vardas turi paskyra ir slaptazodis teisingas."""
    p = _rasti(_skaityti(), vardas)
    if not p:
        return False
    return secrets.compare_digest(_hash(slaptazodis or "", p["druska"]), p["hash"])


def ar_admin(vardas: str) -> bool:
    p = _rasti(_skaityti(), vardas)
    return bool(p and p.get("admin"))


def ar_vadybininkas(vardas: str) -> bool:
    """VADYBININKO paskyra: mato tik 'pas vadybininka' saskaitas, jungia
    eilutes ir raso pardavimo kaina; XML neformuoja — perduoda apskaitai."""
    p = _rasti(_skaityti(), vardas)
    return bool(p and p.get("vadybininkas"))


def vadybininko_folderis(vardas: str) -> str:
    """Vadybininko SAVAS folderis aplinkoje (pvz. Vadybininkas) — jis mato TIK
    jo saskaitas. Tuscia = mato visus vadybininko etapo irasus."""
    p = _rasti(_skaityti(), vardas)
    return (p or {}).get("folderis") or ""


def aplinkos_pavadinimas(vardas: str) -> str:
    """Kurioje APLINKOJE paskyra dirba. Tuscia = savo vardo aplinka (kaip iki
    siol). Vadybininkas dirba TOJE PACIOJE apskaitos aplinkoje (aplinka:
    'apskaita') — duomenys vieni, skiriasi tik rodinys ir teises."""
    p = _rasti(_skaityti(), vardas)
    return (p or {}).get("aplinka") or ""


def pazymeti_prisijungima(vardas: str) -> None:
    paskyros = _skaityti()
    p = _rasti(paskyros, vardas)
    if p:
        p["paskutinis"] = time.time()
        _rasyti(paskyros)


def sukurti(vardas: str, admin: bool = False, aplinka: str = "",
            vadybininkas: bool = False, folderis: str = "") -> str:
    """Sukuria paskyra ir grazina SUGENERUOTA slaptazodi (rodomas viena karta).
    Pirmoji paskyra visada admin. `aplinka` — kitos aplinkos vardas, jei paskyra
    dirba svetimoje aplinkoje (vadybininkas apskaitos aplinkoje)."""
    vardas = (vardas or "").strip()[:40]
    if not vardas:
        raise ValueError("Tuscias vardas")
    paskyros = _skaityti()
    if _rasti(paskyros, vardas):
        raise ValueError(f"Paskyra '{vardas}' jau yra")
    slaptazodis = _generuoti_slaptazodi()
    druska = secrets.token_hex(16)
    paskyros.append({
        "vardas": vardas,
        "druska": druska,
        "hash": _hash(slaptazodis, druska),
        "admin": bool(admin) or len(paskyros) == 0,
        "aplinka": (aplinka or "").strip()[:40],
        "vadybininkas": bool(vadybininkas),
        "folderis": (folderis or "").strip()[:40],
        "sukurta": time.time(),
        "paskutinis": None,
    })
    _rasyti(paskyros)
    return slaptazodis


def nustatyti_slaptazodi(vardas: str, slaptazodis: str) -> None:
    """Uzdeda PASIRINKTA slaptazodi (kai admin nori paprasto, o ne generuoto)."""
    slaptazodis = (slaptazodis or "").strip()
    if len(slaptazodis) < 4:
        raise ValueError("Slaptazodis per trumpas (bent 4 simboliai)")
    paskyros = _skaityti()
    p = _rasti(paskyros, vardas)
    if not p:
        raise ValueError(f"Paskyros '{vardas}' nera")
    p["druska"] = secrets.token_hex(16)
    p["hash"] = _hash(slaptazodis, p["druska"])
    _rasyti(paskyros)


def naujas_slaptazodis(vardas: str) -> str:
    """Sugeneruoja ir uzdeda nauja slaptazodi; grazina ji (rodomas viena karta)."""
    paskyros = _skaityti()
    p = _rasti(paskyros, vardas)
    if not p:
        raise ValueError(f"Paskyros '{vardas}' nera")
    slaptazodis = _generuoti_slaptazodi()
    p["druska"] = secrets.token_hex(16)
    p["hash"] = _hash(slaptazodis, p["druska"])
    _rasyti(paskyros)
    return slaptazodis


def istrinti(vardas: str) -> bool:
    """Istrina paskyra. Paskutinio admino istrinti neleidzia."""
    paskyros = _skaityti()
    p = _rasti(paskyros, vardas)
    if not p:
        return False
    if p.get("admin") and sum(1 for x in paskyros if x.get("admin")) == 1:
        raise ValueError("Paskutinio administratoriaus istrinti negalima")
    paskyros.remove(p)
    _rasyti(paskyros)
    return True


def keisti_admin(vardas: str, admin: bool) -> None:
    paskyros = _skaityti()
    p = _rasti(paskyros, vardas)
    if not p:
        raise ValueError(f"Paskyros '{vardas}' nera")
    if not admin and p.get("admin") and sum(1 for x in paskyros if x.get("admin")) == 1:
        raise ValueError("Turi likti bent vienas administratorius")
    p["admin"] = bool(admin)
    _rasyti(paskyros)


def sarasas() -> list[dict]:
    """Paskyru sarasas be slaptazodziu duomenu (UI lentelei)."""
    return [{
        "vardas": p["vardas"],
        "admin": bool(p.get("admin")),
        "aplinka": p.get("aplinka") or "",
        "vadybininkas": bool(p.get("vadybininkas")),
        "folderis": p.get("folderis") or "",
        "sukurta": p.get("sukurta"),
        "paskutinis": p.get("paskutinis"),
    } for p in _skaityti()]

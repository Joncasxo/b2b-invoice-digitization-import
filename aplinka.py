"""
APLINKOS — kiekviena paskyra turi SAVO duomenis.

MODELIS:
  Paskyra (apskaitininke) = atskira aplinka: savo prijungtas pastas, savo
  gautos saskaitos, savo apdorojimai, savo atmintis. Kito zmogaus duomenu
  nemato niekas — nei per UI, nei per URL.
  Aplinkos VIDUJE folderiai = PROJEKTU VADOVAI (i juos skirstomos saskaitos
  pagal siunteja) — tai ne vartotojai, o darbo suskirstymas.

  aplinkos/<paskyra>/
      darbuotojai/<projekto vadovas>/*.pdf   gautos saskaitos
      saskaitos/*.json                        apdorojimai (AI rezultatai, parinkimai)
      ikelti/*.pdf                            originalu kopijos perziurai
      pastas.json                             SAVO pasto prijungimas
      istrinti.json                           istrintos (kad pastas negrazintu)
      atmintis.json                           SAVO mokymasis

BENDRA visoms aplinkoms (programos dalis, ne duomenys):
  prekiu katalogas (.npz), eksportas/ (viena eile Pragmos agentui),
  paskyros.json, islaidos.json.
"""

import os
import re
import shutil

_DIR = os.path.dirname(os.path.abspath(__file__))
SAKNIS = os.path.join(_DIR, "aplinkos")

# Paskyros vardas -> saugus folderio vardas (be keliu, be keistu simboliu)
_BLOGI = re.compile(r'[<>:"/\\|?*]+')


def _saugus(vartotojas: str) -> str:
    v = _BLOGI.sub("", os.path.basename((vartotojas or "").strip()))[:40]
    return v or "bendra"


def kelias(vartotojas: str) -> str:
    """Aplinkos folderis (sukuriamas, jei dar nera).

    Raidziu dydis NESVARBUS: prisijungus „Apskaita", kai jau yra „apskaita",
    atidaroma TA PATI aplinka. Kitaip zmogus, suklydes viena raide, pamatytu
    tuscia programa be savo saskaitu (taip ir nutiko 2026-08-11)."""
    v = _saugus(vartotojas)
    if not os.path.isdir(os.path.join(SAKNIS, v)) and os.path.isdir(SAKNIS):
        for esamas in os.listdir(SAKNIS):
            if esamas.lower() == v.lower():
                v = esamas
                break
    a = os.path.join(SAKNIS, v)
    for pa in ("darbuotojai", "saskaitos", "ikelti"):
        os.makedirs(os.path.join(a, pa), exist_ok=True)
    return a


def darbuotojai_dir(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "darbuotojai")


def saskaitos_dir(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "saskaitos")


def ikelti_dir(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "ikelti")


def pastas_failas(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "pastas.json")


def istrinti_failas(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "istrinti.json")


def atmintis_failas(vartotojas: str) -> str:
    return os.path.join(kelias(vartotojas), "atmintis.json")


def komentarai_failas(vartotojas: str) -> str:
    """Vadybininko komentarai (laisko tekstas) prie parsisiustu saskaitu."""
    return os.path.join(kelias(vartotojas), "komentarai.json")


def priedai_dir(vartotojas: str) -> str:
    """Ne-saskaitu laisko priedai (pvz. kainu lenteles) — rodomi prie komentaru."""
    return os.path.join(kelias(vartotojas), "priedai")


def sutartys_failas(vartotojas: str) -> str:
    """Sutartiniai ikainiai pardavimo kainoms (zr. sutartys.py)."""
    return os.path.join(kelias(vartotojas), "sutartys.json")


def visos() -> list[str]:
    """Visu esamu aplinku vardai — fono darbams (pastas, AI) per visas paskyras."""
    if not os.path.isdir(SAKNIS):
        return []
    return sorted(f for f in os.listdir(SAKNIS) if os.path.isdir(os.path.join(SAKNIS, f)))


def migruoti(savininkas: str) -> str:
    """VIENKARTINIS perkelimas: seni bendri duomenys -> nurodytos paskyros aplinka.
    Vykdoma paleidziant serveri; jei aplinkos jau yra — nedaro nieko."""
    if os.path.isdir(SAKNIS) and visos():
        return ""
    seni = [("darbuotojai", "d"), ("saskaitos", "d"), ("ikelti", "d"),
            ("pastas.json", "f"), ("istrinti.json", "f"), ("atmintis.json", "f")]
    if not any(os.path.exists(os.path.join(_DIR, p)) for p, _ in seni):
        return ""
    a = kelias(savininkas)
    perkelta = []
    for pavadinimas, tipas in seni:
        senas = os.path.join(_DIR, pavadinimas)
        naujas = os.path.join(a, pavadinimas)
        if not os.path.exists(senas):
            continue
        if tipas == "d":
            os.makedirs(naujas, exist_ok=True)
            for f in os.listdir(senas):
                sf, nf = os.path.join(senas, f), os.path.join(naujas, f)
                if not os.path.exists(nf):
                    shutil.move(sf, nf)
            try:
                os.rmdir(senas)
            except OSError:
                pass
        elif not os.path.exists(naujas):
            shutil.move(senas, naujas)
        perkelta.append(pavadinimas)
    return f"{savininkas}: {', '.join(perkelta)}" if perkelta else ""

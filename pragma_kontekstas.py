"""
PRAGMOS KONTEKSTAS — is ko rinktis keliant i Pragma: duombazes su sandeliais,
tipais, savomis imonemis, grupemis, statusais, pirkejais, projektais ir
tiekeju numatytaisiais (tiekejo kodas -> dazniausias tipas + sandelis).

Agentas siuncia POST /api/agentas/kontekstas tik pasikeitus (~400 KB) ir bent
karta per para. Saugome PERRASYDAMI, ne suliedami — jei sandelis Pragmoje
dingo, jis dingsta ir cia (instrukcijos taisykle).
"""

import json
import os
import threading
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
_FAILAS = os.path.join(_DIR, "pragma_kontekstas.json")
_UZRAKTAS = threading.Lock()


def issaugoti(duomenys: dict) -> int:
    """Issaugo gauta konteksta; grazina KIEK duombaziu priimta (agentui)."""
    imones = (duomenys or {}).get("imones")
    if not isinstance(imones, list) or not imones:
        raise ValueError("Tuscias arba blogas 'imones' sarasas")
    duomenys["gauta"] = datetime.now().isoformat(timespec="seconds")
    with _UZRAKTAS:
        laik = _FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(duomenys, f, ensure_ascii=False)
        os.replace(laik, _FAILAS)
    return len(imones)


def gauti() -> dict:
    try:
        with open(_FAILAS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def imone(duombaze: str) -> dict | None:
    """Vienos duombazes irasas (sandeliai, tipai, numatyta...) arba None."""
    for im in gauti().get("imones") or []:
        if (im.get("duombaze") or "").strip().upper() == (duombaze or "").strip().upper():
            return im
    return None


def kiek() -> int:
    return len(gauti().get("imones") or [])


# ── NUSTATYMAI: kaip keliam i Pragma (pragma_nustatymai.json) ──────────────
# GRIZTAMUMAS (užsakovo reikalavimas): viskas perjungiama vienu lauku —
#   auto=False        -> XML vel krenta i zmogaus folderi (rankinis importo langas)
#   bandymu_rezimas   -> XML eina i BANDYMAI_* duombaze, tikra neliečiama
#   statusas          -> pradziai "Nepatvirtintas" (apskaitininke tvirtina Pragmoje),
#                        pasitikejus — "Patvirtintas" (preke iskart sandelyje)
_NUSTATYMU_FAILAS = os.path.join(_DIR, "pragma_nustatymai.json")
_TIEKEJU_FAILAS = os.path.join(_DIR, "pragma_tiekejai.json")   # musu mokymasis
NUMATYTI_NUSTATYMAI = {
    "duombaze": "IMONE_2026",   # bendrinis pavyzdys — tikra baze pragma_nustatymai.json
    "bandymu_rezimas": True,
    "auto": True,
    "statusas": "Nepatvirtintas",
    "grupe": "**",
    "savos_imones": {},   # folderis -> Pragmos pardavejo zyme (pvz. {"Vadybininkas": "Pardavejas"}), pildoma admin lange
    "pardavimo_tipas": "Pard. prek. trump. skolon",   # numatytas pardavimo tipas — 96% Pragmos pardavimu (Pragmos agentas 2026-09-06)
    "auto_folderis": "",   # tuscia = XML i saskaitos savo folderi (agentas stebi vadybininko folderi)
}
LAUKAI = ("duombaze", "sandelis", "tipas", "statusas", "sava_imone", "grupe", "kainorastis")


def nustatymai() -> dict:
    n = dict(NUMATYTI_NUSTATYMAI)
    try:
        with open(_NUSTATYMU_FAILAS, encoding="utf-8") as f:
            n.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return n


def issaugoti_nustatymus(pakeitimai: dict) -> dict:
    n = nustatymai()
    for k, v in (pakeitimai or {}).items():
        if k in NUMATYTI_NUSTATYMAI:
            n[k] = v
    with _UZRAKTAS:
        laik = _NUSTATYMU_FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(n, f, ensure_ascii=False, indent=1)
        os.replace(laik, _NUSTATYMU_FAILAS)
    return n


def _tiekejai() -> dict:
    try:
        with open(_TIEKEJU_FAILAS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def ismokti(tiekejo_kodas: str, pasirinkimas: dict) -> None:
    """MOKYMASIS: apskaitininke suformavo XML su tokiu sandeliu/tipu siam tiekejui
    -> kita karta tas pats siuloma pirmiau nei Pragmos istorija."""
    kodas = "".join(ch for ch in str(tiekejo_kodas or "") if ch.isalnum())
    if not kodas:
        return
    irasas = {k: pasirinkimas.get(k) for k in ("sandelis", "tipas") if pasirinkimas.get(k)}
    # Duombaze irgi isimenama — bet TIK tikra (bandomoji kopija numatytosios
    # nekeicia: bandymu rezimui isjungus tiekejas turi eiti i tikra baze)
    db = (pasirinkimas.get("duombaze") or "").strip()
    if db and not db.upper().startswith("BANDYMAI_") and not (imone(db) or {}).get("bandomoji"):
        irasas["duombaze"] = db
    if not irasas:
        return
    with _UZRAKTAS:
        t = _tiekejai()
        t[kodas] = irasas
        laik = _TIEKEJU_FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False, indent=1)
        os.replace(laik, _TIEKEJU_FAILAS)


# ── PASKUTINIAI PASIRINKIMAI per VARTOTOJA ir DUOMBAZE (Pragmos agentas nr.32,
# uzsakovo prasymas 09-08): kaip Pragmos importo lange — kita saskaita atsidaro su tuo,
# ka zmogus rinko paskutini karta. Isimenama patvirtinimo momentu: apskaitai —
# formuojant XML, vadybininkui — perduodant apskaitai. pragma_paskutiniai.json
_PASKUTINIU_FAILAS = os.path.join(_DIR, "pragma_paskutiniai.json")
PASKUTINIU_LAUKAI = ("sandelis", "tipas", "statusas", "sava_imone", "grupe", "kainorastis",
                     "pardavimo_tipas", "pirkejas", "projektas")
LAUKU_VARDAI = {"sandelis": "sandėlis", "tipas": "tipas", "statusas": "statusas", "sava_imone": "sava įmonė",
                "grupe": "prekių grupė", "kainorastis": "kainoraštis"}


def _paskutiniai_visi() -> dict:
    try:
        with open(_PASKUTINIU_FAILAS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def paskutiniai(vartotojas: str, duombaze: str) -> dict:
    v, db = (vartotojas or "").strip(), (duombaze or "").strip().upper()
    if not v or not db:
        return {}
    return dict(_paskutiniai_visi().get(f"{v}|{db}") or {})


def paskutine_duombaze(vartotojas: str) -> str:
    """Kuria TIKRA baze vartotojas rinko paskutini karta (bandymu kopijos neisimenamos)."""
    v = (vartotojas or "").strip()
    return ((_paskutiniai_visi().get(f"{v}|*") or {}).get("duombaze") or "") if v else ""


def isiminti_paskutinius(vartotojas: str, duombaze: str, reiksmes: dict) -> None:
    v, db = (vartotojas or "").strip(), (duombaze or "").strip()
    if not v or not db or not isinstance(reiksmes, dict):
        return
    dabar = datetime.now().isoformat(timespec="seconds")
    with _UZRAKTAS:
        visi = _paskutiniai_visi()
        raktas = f"{v}|{db.upper()}"
        irasas = dict(visi.get(raktas) or {})
        for k in PASKUTINIU_LAUKAI:
            if str(reiksmes.get(k) or "").strip():
                irasas[k] = str(reiksmes[k]).strip()
        irasas["laikas"] = dabar
        visi[raktas] = irasas
        if not db.upper().startswith("BANDYMAI_") and not (imone(db) or {}).get("bandomoji"):
            visi[f"{v}|*"] = {"duombaze": db, "laikas": dabar}
        laik = _PASKUTINIU_FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(visi, f, ensure_ascii=False, indent=1)
        os.replace(laik, _PASKUTINIU_FAILAS)


def pasiulymas(tiekejo_kodas: str = "", folderis: str = "", duombaze: str = "", vartotojas: str = "") -> dict:
    """Ka siulyti apskaitininkei siai saskaitai. Eiliskumas: musu mokymasis
    (ka ji pati rinko siam tiekejui) -> Pragmos tiekeju istorija -> duombazes
    numatytieji. Sava imone — pagal folderi (nustatymai.savos_imones).
    `duombaze` — apskaitininkes PASIRINKTA baze (užsakovo prasymas 09-07: ne tik i
    bandymus): pasiulymas ir sarasai skaiciuojami TAI bazei; nenurodzius —
    bandymu rezime BANDYMAI_*, kitaip tiekejui ismokta arba nustatymu baze."""
    n = nustatymai()
    kodas = "".join(ch for ch in str(tiekejo_kodas or "") if ch.isalnum())
    musu = _tiekejai().get(kodas) if kodas else None
    if (duombaze or "").strip():
        duombaze = duombaze.strip()
        im = imone(duombaze) or {}
    else:
        if n.get("bandymu_rezimas"):
            duombaze = "BANDYMAI_" + n["duombaze"]
        else:
            # tiekejui ismokta baze -> vartotojo paskutine baze -> nustatymu
            duombaze = (musu or {}).get("duombaze") or paskutine_duombaze(vartotojas) or n["duombaze"]
        im = imone(duombaze) or imone(n["duombaze"]) or {}
    numatyta = im.get("numatyta") or {}
    # istorijos/mokymosi reiksmes tinka tik jei jos YRA sios bazes sarasuose
    _sar = {"sandelis": im.get("sandeliai") or [], "tipas": im.get("tipai") or []}
    tinka = lambda k, v: bool(v) and v in _sar[k]
    p = {
        "duombaze": duombaze,
        "sandelis": numatyta.get("sandelis") or "",
        "tipas": numatyta.get("tipas") or "",
        "statusas": n.get("statusas") or numatyta.get("statusas") or "",
        "sava_imone": (n.get("savos_imones") or {}).get(folderis or "", ""),
        "grupe": numatyta.get("grupe") or n.get("grupe") or "**",
        "kainorastis": "",
        "saltinis": "numatyta",
        "pastabos": [],
    }
    # PASKUTINIAI VARTOTOJO PASIRINKIMAI sioje bazeje — pagrindas (kaip Pragmos
    # importo lange); reiksme, kurios bazeje nebeliko, keiciama numatytaja su pastaba
    pask = paskutiniai(vartotojas, duombaze)
    _tikrinami = (("sandelis", "sandeliai"), ("tipas", "tipai"), ("statusas", "statusai"),
                  ("sava_imone", "savos_imones"), ("grupe", "grupes"), ("kainorastis", "kainorasciai"))
    for k, sar in _tikrinami:
        v = (pask.get(k) or "").strip()
        if not v:
            continue
        if v in (im.get(sar) or []):
            p[k] = v
            p["saltinis"] = "paskutinis pasirinkimas"
        else:
            p["pastabos"].append(f"ankstesnis {LAUKU_VARDAI.get(k, k)} „{v}“ šioje bazėje nebegalioja — parinkta numatytoji")
    ist = (im.get("tiekeju_numatytieji") or {}).get(kodas) if kodas else None
    if isinstance(ist, dict):
        upd = {k: ist[k] for k in ("sandelis", "tipas") if tinka(k, ist.get(k))}
        if upd:
            p.update(upd)
            p["saltinis"] = "pragmos istorija"
    if isinstance(musu, dict):
        upd = {k: musu[k] for k in ("sandelis", "tipas") if tinka(k, musu.get(k))}
        if upd:
            p.update(upd)
            p["saltinis"] = "apskaitininkės pasirinkimas"
    # sava imone pagal folderi — tik jei ji YRA sios bazes sarase (kitoje
    # imoneje ta pati zyme gali neegzistuoti -> apskaitininke renkasi pati)
    if p["sava_imone"] and im.get("savos_imones") and p["sava_imone"] not in im["savos_imones"]:
        p["sava_imone"] = ""
    p["bandomoji"] = bool(im.get("bandomoji"))
    return p


def numatyta_duombaze(n: dict | None = None) -> str:
    """Kur keliama, kai niekas neparinkta: bandymu rezime — BANDYMAI_ kopija."""
    n = n or nustatymai()
    return ("BANDYMAI_" + n["duombaze"]) if n.get("bandymu_rezimas") else n["duombaze"]


def _numatyta_duombaze_vartotojui(n: dict, vartotojas: str = "") -> str:
    """Kai niekas neparinkta: bandymu rezime BANDYMAI_*, kitaip — vartotojo
    paskutine tikra baze, o jos nesant — nustatymu."""
    if n.get("bandymu_rezimas"):
        return "BANDYMAI_" + n["duombaze"]
    return paskutine_duombaze(vartotojas) or n["duombaze"]


def _pardavimo_imone(duombaze: str = "", vartotojas: str = "") -> dict:
    """Pardavimo sarasams (pirkejai, projektai, pardavimo tipai): pasirinkta
    duombaze; jei bandomoji kopija ju neturi — tikroji (be BANDYMAI_ priesdelio);
    nenurodzius — numatytoji (bandymu rezime BANDYMAI_*, kaip ir pirkimui)."""
    n = nustatymai()
    db = (duombaze or "").strip() or _numatyta_duombaze_vartotojui(n, vartotojas)
    im = imone(db) or {}
    if not im.get("pirkejai") and db.upper().startswith("BANDYMAI_"):
        im = imone(db[len("BANDYMAI_"):]) or im
    if not im:
        im = imone(n["duombaze"]) or {}
    return im


def sarasai(duombaze: str) -> dict:
    """Is ko rinktis UI select'uose — TIK sarasas, laisvo teksto nera."""
    im = imone(duombaze) or {}
    k = gauti()
    return {
        "duombazes": [{"duombaze": x.get("duombaze"), "pavadinimas": x.get("pavadinimas"),
                       "bandomoji": bool(x.get("bandomoji"))} for x in k.get("imones") or []],
        "sandeliai": im.get("sandeliai") or [],
        "tipai": im.get("tipai") or [],
        "statusai": im.get("statusai") or ["Patvirtintas", "Nepatvirtintas", "Juodraštis"],
        "savos_imones": im.get("savos_imones") or [],
        "grupes": im.get("grupes") or ["**"],
        "kainorasciai": im.get("kainorasciai") or [],
    }


_SEKOS_FAILAS = os.path.join(_DIR, "uzsakymu_seka.json")


def kitas_uzsakymo_nr(bandymai: bool = False) -> str:
    """UNIKALUS musu uzsakymo numeris pardavimui — Pragmos DUBLIKATU raktas
    (KvitasNr, <=35 simb., lotyniskos raides/skaiciai/bruksnys). Eiles tvarka
    per metus; bandymu rezime — su TEST- priesdeliu (Pragmos agentas nr.16)."""
    metai = datetime.now().year
    with _UZRAKTAS:
        try:
            with open(_SEKOS_FAILAS, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, json.JSONDecodeError):
            s = {}
        n = (s.get("n") or 0) + 1 if s.get("metai") == metai else 1
        laik = _SEKOS_FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump({"metai": metai, "n": n}, f)
        os.replace(laik, _SEKOS_FAILAS)
    return f"{'TEST-' if bandymai else ''}UZS-{metai}-{n:05d}"


def pardavimo_sarasai(folderis: str = "", duombaze: str = "", vartotojas: str = "") -> dict:
    """Vadybininkui ir apskaitai: is ko rinktis pardavimui — pirkejai, projektai,
    pardavimo tipai TOS duombazes, i kuria keliamas pirkimas (bandomoji kopija
    be sarasu -> tikroji) + sava imone pagal folderi (pastabos sablonui) +
    vartotojo PASKUTINIAI pasirinkimai (pirkejas, projektas, tipas) sioje bazeje."""
    n = nustatymai()
    db = (duombaze or "").strip() or _numatyta_duombaze_vartotojui(n, vartotojas)
    im = _pardavimo_imone(db, vartotojas)
    tipai = im.get("pardavimo_tipai") or []
    numatytas = n.get("pardavimo_tipas") or ""
    if numatytas not in tipai and tipai:
        numatytas = ""
    pask = paskutiniai(vartotojas, db)
    sut = {(x.get("sutartis") or "").strip().upper() for x in (im.get("projektai_detaliai") or []) if isinstance(x, dict)}
    pask_ok = {}
    if pask.get("pirkejas") in (im.get("pirkejai") or []):
        pask_ok["pirkejas"] = pask["pirkejas"]
    if pask.get("projektas") and (pask["projektas"].upper() in sut or pask["projektas"] in (im.get("projektai") or [])):
        pask_ok["projektas"] = pask["projektas"]
    if pask.get("pardavimo_tipas") in tipai:
        pask_ok["pardavimo_tipas"] = pask["pardavimo_tipas"]
    # PROJEKTAI pagal SUTARTIES NUMERI (Pragmos agentas nr.29): Pragmoje sutarties nr
    # guli Projektai.ProID — butent jis rasomas i dokumenta; pavadinimas — tik
    # parodyti. I XML <Projektas> eina sutarties numeris.
    projektai_det = [p for p in (im.get("projektai_detaliai") or [])
                     if isinstance(p, dict) and (p.get("sutartis") or "").strip()]
    # PIRKEJU apmokejimo terminas dienomis (Firmos.ApmTerminas) — „Apmoketi iki"
    # = Data + terminas; be jo importas skaiciuoja pats
    pirkejai_det = [p for p in (im.get("pirkejai_detaliai") or [])
                    if isinstance(p, dict) and (p.get("pavadinimas") or "").strip()]
    return {
        "duombaze": db,
        # vadybininko duombazes pasirinkimui (jis Pragma juostos nemato)
        "duombazes": sarasai("")["duombazes"],
        "bandymu_rezimas": bool(n.get("bandymu_rezimas")),
        "paskutiniai": pask_ok,
        "pirkejai": im.get("pirkejai") or [],
        "pirkejai_detaliai": [{"pavadinimas": p.get("pavadinimas"), "kodas": p.get("kodas") or "",
                               "apm_terminas_d": int(p.get("apm_terminas_d") or 0)} for p in pirkejai_det],
        "projektai": im.get("projektai") or [],
        "projektai_detaliai": [{"sutartis": (p.get("sutartis") or "").strip(), "pavadinimas": p.get("pavadinimas") or "",
                                "uzsakovas": p.get("uzsakovas") or "", "statusas": str(p.get("statusas") or "")} for p in projektai_det],
        "pardavimo_tipai": tipai,
        "numatytas_tipas": numatytas,
        "sava_imone": (n.get("savos_imones") or {}).get(folderis or "", ""),
    }


def patikrinti_pardavima(p: dict, duombaze: str) -> list[str]:
    """Pries pardavimo XML: pirkejas ir tipas TIK is sarasu; projektas, jei
    nurodytas — irgi. Pirkejo nera sarase = jo nera Pragmoje -> stabdom pas mus."""
    im = _pardavimo_imone(duombaze)
    klaidos = []
    pirk = (p.get("pirkejas") or "").strip()
    if not pirk:
        klaidos.append("Pardavimo pirkėjas neparinktas")
    elif pirk not in (im.get("pirkejai") or []):
        klaidos.append(f"Pirkėjo „{pirk}“ Pragmoje nėra — pirmiausia jį reikia užregistruoti Pragmoje")
    tipas = (p.get("tipas") or "").strip()
    if not tipas:
        klaidos.append("Pardavimo tipas neparinktas")
    elif tipas not in (im.get("pardavimo_tipai") or []):
        klaidos.append(f"Pardavimo tipo „{tipas}“ nėra sąraše")
    proj = (p.get("projektas") or "").strip()
    if proj:
        # sutarties numeris (projektai_detaliai.sutartis) ARBA senas pavadinimas
        sutartys = {(x.get("sutartis") or "").strip().upper() for x in (im.get("projektai_detaliai") or []) if isinstance(x, dict)}
        if proj.upper() not in sutartys and proj not in (im.get("projektai") or []):
            klaidos.append(f"Projekto (sutarties) „{proj}“ Pragmoje nėra")
    return klaidos


def patikrinti(p: dict) -> list[str]:
    """Pries XML: kiekviena reiksme PRIVALO buti kontekste (instrukcijos
    taisykle: 'mazmena' vs 'Mažmena' = saskaita klaidose). Grazina klaidu sarasa."""
    klaidos = []
    duombaze = (p or {}).get("duombaze") or ""
    im = imone(duombaze)
    if not im:
        return [f"Nežinoma duombazė „{duombaze}“ — nėra Pragmos kontekste"]
    tikrinti = (("sandelis", "sandeliai", "Sandėlis"), ("tipas", "tipai", "Tipas"),
                ("statusas", "statusai", "Statusas"), ("sava_imone", "savos_imones", "Sava įmonė"),
                ("grupe", "grupes", "Prekių grupė"))
    for laukas, sarasas, vardas in tikrinti:
        reiksme = (p.get(laukas) or "").strip()
        if not reiksme:
            klaidos.append(f"{vardas} neparinkta")
        elif reiksme not in (im.get(sarasas) or []):
            klaidos.append(f"{vardas} „{reiksme}“ nėra duombazės {duombaze} sąraše")
    kr = (p.get("kainorastis") or "").strip()
    if kr and kr not in (im.get("kainorasciai") or []):
        klaidos.append(f"Kainoraštis „{kr}“ nėra sąraše")
    return klaidos

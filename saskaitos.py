"""
ISSAUGOTOS SASKAITOS — karta istraukta saskaita saugoma faile (saskaitos/{id}.json)
ir atidaroma akimirksniu BE AI. Redagavimai (laukai, korteliu parinkimai) irgi saugomi.

id = PDF turinio sha256 pradzia -> tas pats failas niekada neapdorojamas AI antra karta.

APLINKOS: kiekviena funkcija gauna `dir` — tos paskyros saskaitu folderi
(zr. aplinka.py). Kitos paskyros duomenu pasiekti neimanoma.
"""

import json
import os
import secrets
import threading
from datetime import datetime

# "Paėmiau" žymė: kai keli žmonės dirba TOJE PAČIOJE paskyroje (bendra pašto
# dėžutė), sąskaitą vienu metu tvarko tik vienas. Žymė sensta po UZEMIMO_MIN
# minučių be atnaujinimo (pamirštas atidarytas langas neužrakina amžinai).
UZEMIMO_MIN = 30
_UZEMIMO_UZRAKTAS = threading.Lock()


def _kelias(dir: str, saskaitos_id: str) -> str:
    saugus = "".join(c for c in str(saskaitos_id) if c.isalnum())[:32]
    return os.path.join(dir, f"{saugus}.json")


def _dabar() -> str:
    return datetime.now().isoformat(timespec="seconds")


def issaugoti(dir: str, irasas: dict, atnaujinti_laika: bool = True) -> None:
    os.makedirs(dir, exist_ok=True)
    # Užėmimo žymė laiko nekeičia — kitaip vien atidarymas maišytų sąrašo tvarką
    if atnaujinti_laika or not irasas.get("atnaujinta"):
        irasas["atnaujinta"] = _dabar()
    irasas.setdefault("sukurta", irasas["atnaujinta"])
    # Per laikina faila: fone apdorojant kelias saskaitas vienu metu, sarasa
    # skaitantis vartotojas niekada nepagauna pusiau irasyto JSON.
    galutinis = _kelias(dir, irasas["id"])
    # Laikinas failas su UNIKALIU vardu: fonas ir narsykle ta pati irasa gali
    # saugoti tuo paciu metu (09-08 buvo 500 — abu rase i vieną .tmp)
    laik = f"{galutinis}.{secrets.token_hex(4)}.tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump(irasas, f, ensure_ascii=False, indent=1)
    os.replace(laik, galutinis)


def gauti(dir: str, saskaitos_id: str) -> dict | None:
    k = _kelias(dir, saskaitos_id)
    if not os.path.exists(k):
        return None
    try:
        with open(k, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atnaujinti_busena(dir: str, saskaitos_id: str, saskaita: dict, eilutes: list) -> bool:
    """Vartotojo redagavimai: antrastes laukai + eilutes (su parinkimais)."""
    irasas = gauti(dir, saskaitos_id)
    if not irasas:
        return False
    irasas["saskaita"] = saskaita
    irasas["eilutes"] = eilutes
    issaugoti(dir, irasas)
    return True


def pazymeti_xml(dir: str, saskaitos_id: str) -> None:
    irasas = gauti(dir, saskaitos_id)
    if irasas:
        irasas["xml_sugeneruota"] = True
        irasas.pop("tvarko", None)   # darbas baigtas — žymė nebereikalinga
        # Naujas XML = naujas siuntimas: senas Pragmos importo rezultatas (pvz.
        # iš BANDYMŲ bazės ar klaida) nebegalioja — laukiam naujo
        irasas.pop("pragmos_importas", None)
        issaugoti(dir, irasas)


def _norm_kas(kas) -> str:
    return str(kas or "").strip()[:40]


def _tvarko_aktyvi(irasas: dict) -> dict | None:
    """Gyva 'tvarko' žymė arba None (nėra / pasenusi)."""
    t = irasas.get("tvarko") or {}
    if not t.get("kas"):
        return None
    try:
        kada = datetime.fromisoformat(t.get("kada") or "")
    except ValueError:
        return None
    if (datetime.now() - kada).total_seconds() > UZEMIMO_MIN * 60:
        return None
    return t


def uzimti(dir: str, saskaitos_id: str, kas) -> dict:
    """Pažymi 'tvarko <kas>'. Jei jau tvarko KITAS žmogus — nepavyksta ir
    grąžinamas jo vardas. Tas pats žmogus žymę tik atnaujina."""
    kas = _norm_kas(kas) or "kolegė"
    with _UZEMIMO_UZRAKTAS:
        irasas = gauti(dir, saskaitos_id)
        if not irasas:
            return {"pavyko": False, "kas": ""}
        t = _tvarko_aktyvi(irasas)
        if t and t["kas"] != kas:
            return {"pavyko": False, "kas": t["kas"]}
        irasas["tvarko"] = {"kas": kas, "kada": _dabar()}
        issaugoti(dir, irasas, atnaujinti_laika=False)
        return {"pavyko": True, "kas": kas}


def atlaisvinti(dir: str, saskaitos_id: str, kas) -> bool:
    """Nuima žymę (uždarius sąskaitą). Svetimos gyvos žymės nenuima."""
    with _UZEMIMO_UZRAKTAS:
        irasas = gauti(dir, saskaitos_id)
        if not irasas or not irasas.get("tvarko"):
            return True
        t = _tvarko_aktyvi(irasas)
        if t and t["kas"] != _norm_kas(kas):
            return False
        irasas.pop("tvarko", None)
        issaugoti(dir, irasas, atnaujinti_laika=False)
        return True


def kas_tvarko(dir: str, saskaitos_id: str) -> str:
    """Kieno gyva žymė ant sąskaitos ('' — niekieno)."""
    t = _tvarko_aktyvi(gauti(dir, saskaitos_id) or {})
    return t["kas"] if t else ""


def sujungti_i_paketa(dir: str, ids: list, pavadinimas: str = "") -> str:
    """VADYBININKO rankinis paketas: kelios saskaitos sudedamos i viena paketa
    (apskaita jas matys kaip viena 📦 eilute). Grazina paketo id."""
    irasai = []
    for sid in ids:
        irasas = gauti(dir, sid)
        if not irasas:
            raise ValueError(f"Saskaitos {sid} nera")
        irasai.append(irasas)
    pid = secrets.token_hex(5)
    failai = [(i.get("saltinis") or {}).get("failas") or i.get("id") for i in irasai]
    for irasas in irasai:
        kom = irasas.get("vadybininko_komentarai") or {}
        kom["paketas"] = pid
        kom["kartu"] = failai
        kom["rankinis"] = True
        if pavadinimas:
            kom["tema"] = pavadinimas
        irasas["vadybininko_komentarai"] = kom
        issaugoti(dir, irasas, atnaujinti_laika=False)
    return pid


def isardyti_paketa(dir: str, paketo_id: str) -> int:
    """Isima paketo rysi is visu jo saskaitu (pacios saskaitos, tekstai ir
    priedai lieka). Grazina kiek irasu atnaujinta."""
    kiek = 0
    if not os.path.isdir(dir):
        return 0
    for f in os.listdir(dir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir, f), encoding="utf-8") as fh:
                irasas = json.load(fh)
        except Exception:
            continue
        kom = irasas.get("vadybininko_komentarai") or {}
        if kom.get("paketas") == paketo_id:
            kom.pop("paketas", None)
            kom.pop("kartu", None)
            kom.pop("rankinis", None)
            irasas["vadybininko_komentarai"] = kom
            issaugoti(dir, irasas, atnaujinti_laika=False)
            kiek += 1
    return kiek


def perduoti(dir: str, saskaitos_id: str, kas, atgal: bool = False) -> bool:
    """VADYBININKO srautas: perduoda saskaita apskaitai (etapas nuimamas) arba
    atsiima atgal (atgal=True). Pinigu/eiluciu neliecia."""
    irasas = gauti(dir, saskaitos_id)
    if not irasas:
        return False
    if atgal:
        irasas["etapas"] = "vadybininkas"
        irasas.pop("perdave", None)
    else:
        irasas.pop("etapas", None)
        irasas["perdave"] = {"kas": _norm_kas(kas), "kada": _dabar()}
    issaugoti(dir, irasas)
    return True


def trinti(dir: str, saskaitos_id: str) -> str | None:
    """Pasalina issaugota saskaita; grazina failo_id (ikelti/ kopijos valymui) arba None."""
    irasas = gauti(dir, saskaitos_id)
    if not irasas:
        return None
    try:
        os.remove(_kelias(dir, saskaitos_id))
    except OSError:
        return None
    return irasas.get("failo_id")


def visos(dir: str) -> list[dict]:
    """Santraukos sarasui pradiniame ekrane.

    Tvarka: PIRMA tos, kurioms XML dar nesuformuotas (jos dar laukia darbo),
    po ju — jau israsytos. Abiejose grupese naujausios virsuje."""
    out = []
    if not os.path.isdir(dir):
        return out
    for f in os.listdir(dir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir, f), encoding="utf-8") as fh:
                i = json.load(fh)
        except Exception:
            continue
        s = i.get("saskaita") or {}
        eil = i.get("eilutes") or []
        neto = round(sum(float(e.get("suma") or 0) for e in eil), 2)
        out.append({
            "id": i.get("id"),
            "tiekejas": (s.get("tiekejas") or {}).get("pavadinimas") or "—",
            "tiekejo_kodas": (s.get("tiekejas") or {}).get("imones_kodas") or "",
            "numeris": s.get("saskaitos_numeris") or "—",
            "data": s.get("saskaitos_data") or "",
            "suma_be_pvm": neto,
            "eiluciu": len(eil),
            "saltinis": i.get("saltinis") or {},
            "xml_sugeneruota": bool(i.get("xml_sugeneruota")),
            "atnaujinta": i.get("atnaujinta") or "",
            "tvarko": (_tvarko_aktyvi(i) or {}).get("kas", ""),
            # paketas: to paties laisko saskaitos sarase rodomos viena eilute
            "papildyta": bool(i.get("papildyta")),
            # ETAPAS: "vadybininkas" = dar pas vadybininka (apskaita nemato,
            # kol jis nepaspaudzia "Perduoti apskaitai"); tuscia = apskaitoje
            "etapas": i.get("etapas") or "",
            # Pragmos importo GALUTINE busena (ikelta/dublikatas/klaida) is agento
            "pragmos_importas": i.get("pragmos_importas") or None,
            # ATSKIRO pardavimo (Document-Sale) importo busena + musu uzsakymo nr
            "pragmos_pardavimo_importas": i.get("pragmos_pardavimo_importas") or None,
            "uzsakymo_nr": (i.get("pardavimo_uzsakymas") or {}).get("nr") or "",
            "paketas": (i.get("vadybininko_komentarai") or {}).get("paketas") or "",
            "paketo_tema": (i.get("vadybininko_komentarai") or {}).get("tema") or "",
            # rankinis = vadybininko sudetas paketas (jo sarase grupuojamas tik toks)
            "rankinis_paketas": bool((i.get("vadybininko_komentarai") or {}).get("rankinis")),
            "priedu": len((i.get("vadybininko_komentarai") or {}).get("priedai") or []),
        })
    # Tvarka pagal SASKAITOS data (ne atsiuntimo): seniausios virsuje — jas
    # israsyti reikia pirmiausia. Be datos — grupes apacioje.
    out.sort(key=lambda x: x["data"] or "9999-99-99")
    out.sort(key=lambda x: x["xml_sugeneruota"])   # False (0) — i virsu
    return out

"""
Uzpajamavimo DEMO serveris (atskiras nuo SaaS, tik localhost).

Srautas:
  1) POST /api/ikelti        — PDF/nuotrauka -> AI ekstrakcija (Haiku) + kodo validacija
                               + katalogo rekomendacijos kiekvienai eilutei (katalogo paieskos variklis)
  2) POST /api/paieska       — rankine paieska kataloge (kai rekomendacija netinka)
  3) POST /api/xml           — patikrinti duomenys -> Document-Invoice XML (grynas kodas, 0 tokenu)

Paleisti:  py -m uvicorn server:app --port 8300
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from contextlib import asynccontextmanager

try:
    import truststore
    truststore.inject_into_ssl()  # Norton/Windows sertifikatai — kad nekirstu HTTPS
except Exception:
    pass

# .env uzkrovimas PRIES importuojant modulius, kurie skaito raktus
_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_DIR, ".env")):
    for _eil in open(os.path.join(_DIR, ".env"), encoding="utf-8"):
        _eil = _eil.strip()
        if _eil and not _eil.startswith("#") and "=" in _eil:
            _k, _v = _eil.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from openai import OpenAI
from pydantic import BaseModel

import atmintis
import eksportas
import ekstrakcija
import pastas
import prisijungimas
import paskyros
import islaidos
import klasifikacija
import aplinka
import pragma_dokumentai
import pragma_importai
import pragma_kontekstas
import pragma_zinutes
import priedu_perziura
import saskaitos
import sutartys
import xml_generavimas
from paieska_engine import Katalogas, tekstas_be_skaiciu

@asynccontextmanager
async def _gyvavimas(_app):
    """Paleidus serveri startuoja fono darbininkas (AI skaito naujus PDF pats)."""
    _pritaikyti_papildymus()   # katalogas + tai, ka Pragma praneše po eksporto
    uzduotis = asyncio.create_task(_fono_ciklas())
    yield
    uzduotis.cancel()


app = FastAPI(title="Uzpajamavimo demo", lifespan=_gyvavimas)

# GZIP: JSON sarasai ant leto tinklo (hotspot) susitraukia ~5-10 kartu
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ── Prieigos kontrole (kai PRISIJUNGIMO_SLAPTAZODIS nustatytas .env) ────────
# Lokaliai (slaptazodis tuscias) viskas veikia kaip anksciau, be prisijungimo.
_ATVIRI_KELIAI = ("/login", "/api/prisijungti", "/favicon.ico",
                  "/manifest.json", "/sw.js",
                  "/static/ikona-192.png", "/static/ikona-512.png")


@app.middleware("http")
async def _prieiga(request: Request, call_next):
    kelias = request.url.path
    if kelias.startswith("/api/agentas/"):
        # Pragmos serverio agentas — atskiras raktas antrasteje, ne cookie
        if not prisijungimas.agento_raktas_geras(request.headers.get("X-Agento-Raktas") or ""):
            return JSONResponse({"detail": "Blogas arba nenustatytas AGENTO_RAKTAS"}, status_code=401)
        return await call_next(request)
    # LAIKINAS UZRAKTAS (.env TIK_SIE_IP): programa pasiekiama tik isvardintiems
    # irenginiams (Tailscale IP). Tuscia reiksme = visiems, kaip iprastai.
    leisti_ip = [x.strip() for x in (os.environ.get("TIK_SIE_IP") or "").split(",") if x.strip()]
    if leisti_ip:
        klientas = request.client.host if request.client else ""
        if klientas not in leisti_ip:
            if kelias.startswith("/api/"):
                return JSONResponse({"detail": "Programa laikinai išjungta priežiūrai"}, status_code=403)
            return HTMLResponse("<div style='font-family:system-ui; text-align:center; margin-top:20vh'>"
                                "<h2>🔧 Programa laikinai išjungta priežiūrai</h2>"
                                "<p>Pabandyk vėliau.</p></div>", status_code=403)
    if prisijungimas.ijungtas() and kelias not in _ATVIRI_KELIAI:
        vardas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
        if not vardas:
            if kelias.startswith("/api/"):
                return JSONResponse({"detail": "Neprisijungta"}, status_code=401)
            return RedirectResponse("/login")
        # SLENKANTI SESIJA: kol dirbama, galiojimas atsinaujina kiekvienu
        # uzklausimu; pabuvus be veiklos ilgiau nei SESIJOS_MIN — login is naujo.
        # (atsijungimo atsakymo neliesti — jis cookie kaip tik trina)
        atsakas = await call_next(request)
        # Cookie atnaujinamas tik API atsakymuose — PDF/statikos atsakymu
        # nesunkinam (Set-Cookie ant ju trukdo narsykles kesui)
        if kelias.startswith("/api/") and kelias != "/api/atsijungti":
            atsakas.set_cookie("sesija", prisijungimas.sesijos_cookie(vardas), httponly=True,
                               samesite="lax", secure=prisijungimas.saugus_cookie())
        return atsakas
    return await call_next(request)

# ── APLINKOS: kiekviena paskyra turi SAVO duomenis ──────────────────────────
# Paskyra (apskaitininke) = savo pastas, savo saskaitos, savo atmintis.
# Aplinkos viduje folderiai = PROJEKTU VADOVAI (skirstymas pagal siunteja).
# Bendra visiems: prekiu katalogas, eksporto eile agentui, paskyros, islaidos.
_migracija = aplinka.migruoti(os.environ.get("APLINKOS_SAVININKAS") or "Savininkas")
if _migracija:
    print(f"[aplinkos] seni duomenys perkelti -> {_migracija}", flush=True)

def _pradiniai_folderiai(darb_dir: str) -> None:
    """Nauja aplinka gauna pradinius PROJEKTU VADOVU folderius is .env
    (DARBUOTOJAI=Petras,Ona) — tik jei ju dar nera. Toliau valdoma per UI."""
    os.makedirs(darb_dir, exist_ok=True)
    if any(os.path.isdir(os.path.join(darb_dir, x)) for x in os.listdir(darb_dir)):
        return
    for v in [x.strip() for x in (os.environ.get("DARBUOTOJAI") or "").split(",") if x.strip()]:
        os.makedirs(os.path.join(darb_dir, v), exist_ok=True)

MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# ── Katalogas (uzkraunamas karta paleidus) ──────────────────────────────────
KATALOGAS: Katalogas | None = None
KATALOGO_KLAIDA = ""


def _uzkrauti_kataloga():
    global KATALOGAS, KATALOGO_KLAIDA
    kelias = os.environ.get("KATALOGO_NPZ") or os.path.join(_DIR, "testinis_katalogas.npz")
    if not os.path.exists(kelias):
        KATALOGO_KLAIDA = f"Katalogo failo nera: {kelias}. Paleisk: py sukurti_testini_kataloga.py"
        return
    try:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60)
        KATALOGAS = Katalogas(kelias, client)
        print(f"[katalogas] uzkrauta {KATALOGAS.N} korteliu is {os.path.basename(kelias)}")
    except Exception as e:
        KATALOGO_KLAIDA = f"Nepavyko uzkrauti katalogo: {e}"


_uzkrauti_kataloga()


# ── Pagalbines ──────────────────────────────────────────────────────────────
def _norm_numeris(nr) -> str:
    """Saskaitos numeris apskaitai — SUTRAUKTAS: be tarpu ir bruksniu (AAA 3011090 -> AAA3011090).
    Zodelis „Nr." nera numerio dalis (Serija REG Nr. 35018371 -> REG35018371)."""
    t = re.sub(r"(?i)\bnr\.?(?=[\s\d]|$)", "", str(nr or ""))
    return re.sub(r"[\s\-]+", "", t)


REKOMENDACIJU = 15   # kiek korteliu siuloma eilutei (sarasas UI slenkamas)


def _sulieti_rekomendacijas(a: list | None, b: list | None, kiek: int = REKOMENDACIJU) -> list:
    """Dvieju paiesku (dokumento uzrasas + AI bendrinis terminas) rezultatai -> vienas
    sarasas: kiekvienai kortelei imamas GERESNIS balas, rusiuojama is naujo."""
    geriausi: dict[str, dict] = {}
    for r in (a or []) + (b or []):
        k = r["kodas"]
        if k not in geriausi or r["balas"] > geriausi[k]["balas"]:
            geriausi[k] = r
    rez = sorted(geriausi.values(), key=lambda r: -r["balas"])[:kiek]
    virsus = rez[0]["balas"] if rez else 0
    for r in rez:
        r["zemiau_luzio"] = bool(virsus > 0 and r["balas"] < virsus * 0.5)
    return rez


def _pilna_ekstrakcija(apl: "Aplinka", baitai: bytes, mime: str, failo_id: str) -> dict:
    """AI + validacija + rekomendacijos + atmintis (be issaugojimo).
    `apl` — tos paskyros aplinka (atmintis imama TIK is jos)."""
    try:
        d = ekstrakcija.istraukti(baitai, mime)
    except Exception as e:
        raise HTTPException(502, f"AI ekstrakcija nepavyko: {e}")

    ispejimai = ekstrakcija.patikrinti(d)
    eilutes = d.get("eilutes") or []

    tiekejas = {
        "pavadinimas": d.get("tiekejas_pavadinimas") or "",
        "imones_kodas": d.get("tiekejas_imones_kodas") or "",
        "pvm_kodas": d.get("tiekejas_pvm_kodas") or "",
        "gatve": d.get("tiekejas_gatve") or "",
        "miestas": d.get("tiekejas_miestas") or "",
        "pasto_kodas": d.get("tiekejas_pasto_kodas") or "",
    }

    # Rekomendacijos + SINONIMU SLUOKSNIS (rozetes tipo atvejai, kai tiekejas ir
    #   katalogas ta pati prekes tipa vadina skirtingais zodziais):
    #   1) eiluciu vektoriai (1 embedding kvietimas)
    #   2) is artimiausiu korteliu surenkami KATALOGO terminai
    #   3) mazas AI kvietimas: AI RENKASI is tu terminu (ne kuria — be haliucinaciju)
    #   4) paieska dokumento uzrasu IR perrasytu pavadinimu — imamas geresnis balas
    rekomendacijos: list[list] = []
    if KATALOGAS and eilutes:
        try:
            pavadinimai = [e.get("pavadinimas") or "" for e in eilutes]
            vienetai = [e.get("vienetas") or "" for e in eilutes]
            vv = KATALOGAS.embeddingai([tekstas_be_skaiciu(p) or "preke" for p in pavadinimai])

            # Pirmine paieska dokumento uzrasais — is jos matosi, kurioms eilutems
            # paieska ir taip randa (sinonimu AI ten nereikalingas).
            pirminiai = [KATALOGAS.ivertinti(pavadinimai[i], vv[i], REKOMENDACIJU, vienetas=vienetai[i])
                         for i in range(len(eilutes))]

            # Sinonimu AI kvieciamas TIK SILPNOMS eilutems (top-1 balas zemas) —
            # butent ten buna zodyno spragos (tiekejas ir katalogas ta pati daikta
            # vadina skirtingai). Stiprioms eilutems tai butu pinigu svaistymas.
            SIN_RIBA = 1.0
            silpnos = [i for i, a in enumerate(pirminiai) if not a or a[0]["balas"] < SIN_RIBA]
            sin: list[str] = [""] * len(eilutes)
            if silpnos:
                try:
                    terminai_s = [KATALOGAS.terminai_pagal_vektoriu(vv[i]) for i in silpnos]
                    sin_s, sin_naud = ekstrakcija.sinonimai_pagal_terminus(
                        [pavadinimai[i] for i in silpnos], terminai_s)
                    # APSAUGOS nuo AI nuklydimo perrasant sarasa vienu kartu:
                    # 1) „sinonimas", sutampantis su KITA tos pacios saskaitos eilute —
                    #    pasislinkusio saraso pozymis (kopijuoja kaimyna), ne sinonimas;
                    # 2) sinonimas privalo naudoti termina is jam duoto katalogo saraso —
                    #    kitaip tai laisva kuryba, o ne pasirinkimas is duotu terminu.
                    def _norm_sin(t):
                        return re.sub(r"\s+", " ", (t or "").strip().lower())
                    pav_norm = [_norm_sin(p) for p in pavadinimai]
                    for j, i in enumerate(silpnos):
                        s = (sin_s[j] if j < len(sin_s) else "") or ""
                        if not s:
                            continue
                        ns = _norm_sin(s)
                        if any(ns == pav_norm[k] for k in range(len(pav_norm)) if k != i):
                            continue
                        terms = [t.lower() for t in terminai_s[j] if t]
                        if terms and not any(t in ns for t in terms):
                            continue
                        sin[i] = s
                    ai = d.get("_ai") or {}
                    ai["tokenai_in"] = ai.get("tokenai_in", 0) + sin_naud["tokenai_in"]
                    ai["tokenai_out"] = ai.get("tokenai_out", 0) + sin_naud["tokenai_out"]
                    ai["kaina_ct"] = ekstrakcija.kaina_ct(ai.get("modelis") or ekstrakcija.MODELIS, ai["tokenai_in"], ai["tokenai_out"])
                    d["_ai"] = ai
                except Exception as e:
                    print(f"[sinonimai] nepavyko: {e}")

            # Apsauga: jei AI perrasyme pamete parametrus (skaicius) — pridedam is originalo,
            #   kad skaiciu palyginimas (dydziai/galia) liktu saziningas.
            for i, s in enumerate(sin):
                if s:
                    orig_sk = re.findall(r"\d+(?:[.,]\d+)?(?:x\d+)?", pavadinimai[i])
                    if orig_sk and not re.search(r"\d", s):
                        sin[i] = s + " " + " ".join(orig_sk)
            for e, s in zip(eilutes, sin):
                e["bendrinis_pavadinimas"] = s

            sin_idx = [i for i, s in enumerate(sin) if s]
            sin_vv = KATALOGAS.embeddingai([tekstas_be_skaiciu(sin[i]) or "preke" for i in sin_idx]) if sin_idx else []
            sin_vieta = {idx: j for j, idx in enumerate(sin_idx)}

            for i in range(len(eilutes)):
                b = KATALOGAS.ivertinti(sin[i], sin_vv[sin_vieta[i]], REKOMENDACIJU, vienetas=vienetai[i]) if i in sin_vieta else None
                rekomendacijos.append(_sulieti_rekomendacijas(pirminiai[i], b))
        except Exception as e:
            ispejimai.append(f"Katalogo paieska nepavyko: {e}")
    elif not KATALOGAS:
        ispejimai.append(KATALOGO_KLAIDA or "Katalogas neuzkrautas.")

    # ATMINTIS (0 tokenu): ar sito tiekejo prekes jau buvo priskirtos pr kortelems?
    atm = atmintis.rasti_visiems(apl.atmintis, tiekejas, eilutes)

    eil_out = []
    for i, e in enumerate(eilutes):
        eil_out.append({
            "pavadinimas": e.get("pavadinimas") or "",
            "bendrinis_pavadinimas": e.get("bendrinis_pavadinimas") or "",
            "tiekejo_kodas": e.get("tiekejo_kodas") or "",
            "ean": e.get("ean") or "",
            "kiekis": e.get("kiekis"),
            "vienetas": e.get("vienetas") or "vnt",
            "vnt_kaina": e.get("vnt_kaina"),
            "suma": e.get("suma"),
            "pvm_proc": e.get("pvm_proc") if e.get("pvm_proc") is not None else 21,
            "rekomendacijos": rekomendacijos[i] if i < len(rekomendacijos) else [],
            "atmintis": atm[i] if i < len(atm) else None,
        })

    rez = {
        "failo_id": failo_id,
        "mime": mime,
        "yra_saskaita": d.get("yra_pirkimo_saskaita", True),
        "dokumento_tipas": d.get("dokumento_tipas") or "",
        "ai": d.get("_ai", {}),
        "ispejimai": ispejimai,
        "saskaita": {
            "tiekejas": tiekejas,
            "pirkejas": {
                "pavadinimas": d.get("pirkejas_pavadinimas") or "",
                "imones_kodas": d.get("pirkejas_imones_kodas") or "",
                "pvm_kodas": d.get("pirkejas_pvm_kodas") or "",
                "miestas": d.get("pirkejas_miestas") or "",
            },
            "saskaitos_numeris": _norm_numeris(d.get("saskaitos_numeris")),
            "saskaitos_data": d.get("saskaitos_data") or "",
            "apmoketi_iki": d.get("apmoketi_iki") or "",
            "valiuta": d.get("valiuta") or "EUR",
            "suma_be_pvm": d.get("suma_be_pvm"),
            "pvm_suma": d.get("pvm_suma"),
            "suma_su_pvm": d.get("suma_su_pvm"),
        },
        "eilutes": eil_out,
    }
    # PRADINE BUSENA: gili kopija vieno mygtuko atstatymui po bet kokiu
    # redagavimu (sujungimu, isskaidymu, parinkimu) — 0 tokenu, be AI
    rez["originalas"] = json.loads(json.dumps({"saskaita": rez["saskaita"],
                                               "eilutes": rez["eilutes"]}))
    return rez


# Pasiulymo eiluciu susiejimas ISIMTAS VISAI (uzsakovo sprendimas, 2026-08-30) — gris
# vadybininku etape ju lange. pasiulymas.py modulis liko diske nekvieciamas.
def _musu_imone(kodas) -> bool:
    """Ar imones kodas — viena is MUSU imoniu (Pragmos konteksto imones[]).
    Naudojama apsaugai: musu paciu israsyta pardavimo saskaita ne pirkimas."""
    k = re.sub(r"\D", "", str(kodas or ""))
    if not k:
        return False
    return any(re.sub(r"\D", "", str(im.get("imones_kodas") or "")) == k
               for im in (pragma_kontekstas.gauti().get("imones") or []))


def _vadybininko_folderiai() -> set:
    """Folderiai, kuriu saskaitos pirmiausia keliauja VADYBININKUI (.env
    VADYBININKO_FOLDERIAI=Vadybininkas,Testas). Tuscia = srautas isjungtas."""
    return {f.strip() for f in (os.environ.get("VADYBININKO_FOLDERIAI") or "").split(",") if f.strip()}


def _apdoroti(apl: "Aplinka", baitai: bytes, pavadinimas: str, saltinis: dict | None = None) -> dict:
    """Pilnas kelias: jei tas pats failas JAU apdorotas — grazina ISSAUGOTA (0 tokenu, be AI).
    Kitaip: AI ekstrakcija + issaugojimas i tos paskyros saskaitos/."""
    ext = os.path.splitext(pavadinimas or "f.pdf")[1].lower() or ".pdf"
    mime = MIME.get(ext)
    if not mime:
        raise HTTPException(400, f"Nepalaikomas failo tipas: {ext} (galima: PDF, JPG, PNG)")

    saskaitos_id = hashlib.sha256(baitai).hexdigest()[:12]
    issaugota = saskaitos.gauti(apl.sask, saskaitos_id)
    if issaugota:
        issaugota["is_issaugotos"] = True  # UI zinutei: atidaryta be AI
        return issaugota

    failo_id = saskaitos_id + ext
    os.makedirs(apl.ikelti, exist_ok=True)
    with open(os.path.join(apl.ikelti, failo_id), "wb") as f:
        f.write(baitai)

    rez = _pilna_ekstrakcija(apl, baitai, mime, failo_id)
    rez["id"] = saskaitos_id
    rez["saltinis"] = saltinis or {"tipas": "ikelta", "failas": pavadinimas}
    rez["xml_sugeneruota"] = False
    # VADYBININKO ETAPAS: saskaita is vadybininko folderio pirmiausia pas ji —
    # apskaita ja pamato tik jam paspaudus "Perduoti apskaitai"
    if rez["saltinis"].get("darbuotojas") in _vadybininko_folderiai():
        rez["etapas"] = "vadybininkas"
    # VADYBININKO KOMENTARAI: laisko tekstas + ne-saskaitu priedai (0 AI) —
    # tik parodyti zmogui prie saskaitos; i XML NEkeliauja
    salt = rez["saltinis"]
    if salt.get("darbuotojas") and salt.get("failas"):
        try:
            with open(apl.komentarai, encoding="utf-8") as f:
                k = json.load(f).get(f"{salt['darbuotojas']}/{salt['failas']}")
            if k:
                rez["vadybininko_komentarai"] = k
        except Exception:
            pass
    saskaitos.issaugoti(apl.sask, rez)
    # islaidu zurnalas adminui: paskyra + projekto vadovo folderis
    try:
        pv = (rez.get("saltinis") or {}).get("darbuotojas")
        islaidos.prideti(f"{apl.vartotojas} / {pv}" if pv else apl.vartotojas,
                         (rez.get("ai") or {}).get("kaina_ct"))
    except Exception:
        pass
    return rez


# ── FONO APDOROJIMAS ────────────────────────────────────────────────────────
# Naujas PDF darbuotojo folderyje (is pasto ar imestas ranka) AI perskaitomas
# IS KARTO fone. Apskaitininke paspaudusi mato jau paruosta saskaita (0 laukimo).
# Isjungti: .env  AUTO_APDOROTI=0
FONO_BUSENA: dict[str, str] = {}    # failo kelias -> "vyksta" arba "klaida: ..."
_FONO_BANDYMAI: dict[str, int] = {}  # nepavykusiu bandymu skaitliukas

# Kiek saskaitu skaitoma VIENU METU (.env LYGIAGRETUMAS).
# Vietos dalinamos SAZININGAI tarp projektu vadovu: kol laukia keli — kiekvienam
# po viena, kad vienas su 20 saskaitu neuzblokuotu kitu. Likus vienam laukianciam
# — visos vietos atitenka jam. Pradedam nuo 5; jei eile dazniau auga, o klaidu
# nera, galima kelti iki 8-10 nekeiciant kodo.
LYGIAGRETUMAS = max(1, int(os.environ.get("LYGIAGRETUMAS") or 5))
_VYKDOMI: dict[str, str] = {}          # failo kelias -> projekto vadovo raktas
_EILES_LAIKAS: dict[str, float] = {}   # raktas -> kada paskutini karta gavo vieta
_UZDUOTYS: set = set()                 # gyvos fono uzduotys (kad ju nesurinktu GC)
_PAKARTOTI: dict[str, float] = {}      # kelias -> nuo kada galima bandyti is naujo
_HASH_ATMINTIS: dict[str, tuple[float, int, str]] = {}  # kelias -> (laikas, dydis, id)


PASTO_KLAIDA = ""      # paskutine pasto tikrinimo klaida (rodoma UI)
_PASTO_LAIKAS = 0.0    # kada paskutini karta tikrintas pastas


async def _fono_ciklas():
    intervalas = max(5, int(os.environ.get("FONO_INTERVALAS") or 15))
    while True:
        try:
            await _fono_pastas()
            if (os.environ.get("AUTO_APDOROTI") or "1") == "1":
                await _fono_praejimas()
        except Exception as e:
            print(f"[fonas] ciklo klaida: {e}")
        await asyncio.sleep(intervalas)


async def _fono_pastas():
    """Automatiskai parsisiuncia naujas saskaitas — KIEKVIENOS paskyros is JOS pasto."""
    global PASTO_KLAIDA, _PASTO_LAIKAS
    if (os.environ.get("AUTO_PASTAS") or "1") != "1":
        return
    intervalas = max(30, int(os.environ.get("PASTO_INTERVALAS") or 120))
    if time.time() - _PASTO_LAIKAS < intervalas:
        return
    _PASTO_LAIKAS = time.time()

    dienos = int(os.environ.get("PASTAS_DIENOS") or 30)
    klaidos = []
    for vartotojas in aplinka.visos():
        apl = Aplinka(vartotojas)
        if not pastas.busena(apl.pastas).get("paskyra"):
            continue   # si paskyra pasto neprijungusi — praleidziam
        try:
            rez = await asyncio.to_thread(pastas.parsisiusti_naujus, apl.pastas, apl.darb, dienos)
            nauji = sum(len(v) for v in (rez.get("folderiai") or {}).values())
            if nauji:
                print(f"[pastas] {vartotojas}: parsisiusta nauju saskaitu: {nauji}", flush=True)
            # PAPILDYTA INFORMACIJA: tas pats PDF atejo dar karta su nauju laisko
            # tekstu/priedais — jau apdorota saskaita gauna naujus komentarus ir ❗ zyme
            for raktas in rez.get("komentaru_atnaujinta") or []:
                try:
                    fold, vardas_f = raktas.split("/", 1)
                    sid = _failo_id(os.path.join(apl.darb, fold, vardas_f))
                    irasas = saskaitos.gauti(apl.sask, sid) if sid else None
                    if not irasas:
                        continue
                    with open(apl.komentarai, encoding="utf-8") as f:
                        kom = json.load(f).get(raktas)
                    if kom and irasas.get("vadybininko_komentarai") != kom:
                        irasas["vadybininko_komentarai"] = kom
                        irasas["papildyta"] = True
                        saskaitos.issaugoti(apl.sask, irasas, atnaujinti_laika=False)
                        print(f"[pastas] {vartotojas}: papildyta informacija saskaitai {vardas_f}", flush=True)
                except Exception as e:
                    print(f"[pastas] papildymo sinchronizacija nepavyko ({raktas}): {e}", flush=True)
        except Exception as e:
            klaidos.append(f"{vartotojas}: {e}")
            print(f"[pastas] {vartotojas} klaida: {e}", flush=True)
    PASTO_KLAIDA = "; ".join(klaidos)[:300]


def _failo_id(kelias: str) -> str | None:
    """Failo turinio id (sha256 pradzia). Isimenam pagal laika+dydi, kad kas
    kelias sekundes nereiketu is naujo perskaityti visu folderiu PDF."""
    try:
        st = os.stat(kelias)
    except OSError:
        return None
    buvo = _HASH_ATMINTIS.get(kelias)
    if buvo and buvo[0] == st.st_mtime and buvo[1] == st.st_size:
        return buvo[2]
    try:
        with open(kelias, "rb") as fh:
            sid = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return None
    _HASH_ATMINTIS[kelias] = (st.st_mtime, st.st_size, sid)
    return sid


def _laukiancios() -> dict[str, list[tuple]]:
    """Dar neapdoroti failai visose paskyrose, sugrupuoti pagal projekto vadova.
    Raktas: "paskyra/folderis". Kiekvienoje eileje seniausia saskaita — pirma."""
    eiles: dict[str, list[tuple]] = {}
    for vartotojas in aplinka.visos():
        apl = Aplinka(vartotojas)
        if not os.path.isdir(apl.darb):
            continue
        for vardas in sorted(os.listdir(apl.darb)):
            folderis = os.path.join(apl.darb, vardas)
            if not os.path.isdir(folderis):
                continue
            # Grieztoje tvarkoje „Nepriskirta" = ne musu saskaitos: guli, bet AI
            # ju neskaito (kitaip mest pinigus uz svetimus dokumentus).
            if vardas == "Nepriskirta" and pastas.griezta_tvarka():
                continue
            for f in sorted(os.listdir(folderis)):
                if os.path.splitext(f)[1].lower() not in MIME:
                    continue
                kelias = os.path.join(folderis, f)
                if kelias in FONO_BUSENA:   # jau vyksta arba galutinai nepavyko
                    continue
                if time.time() < _PAKARTOTI.get(kelias, 0):
                    continue                # neseniai nepavyko — pailsim pries bandant vel
                sid = _failo_id(kelias)
                if not sid or saskaitos.gauti(apl.sask, sid):
                    continue                # jau apdorota anksciau
                try:
                    laikas = os.path.getmtime(kelias)
                except OSError:
                    continue
                eiles.setdefault(f"{vartotojas}/{vardas}", []).append(
                    (laikas, vartotojas, vardas, f, kelias))
    for eile in eiles.values():
        eile.sort(key=lambda x: x[0])       # seniausia — priekyje
    return eiles


async def _fono_praejimas():
    """Uzpildo laisvas apdorojimo vietas.

    Vieta gauna tas projektu vadovas, kuris SENIAUSIAI jos negavo — tada imama
    jo seniausia saskaita. Taip vienas darbuotojas su 20 saskaitu nenustumia
    kitu i eiles gala. Funkcija nelaukia, kol darbai baigsis: atsilaisvinusia
    vieta tuoj pat uzima kitas (zr. _fono_darbas pabaiga)."""
    laisva = LYGIAGRETUMAS - len(_VYKDOMI)
    if laisva <= 0:
        return
    eiles = _laukiancios()
    # Dalinam RATAIS: pirmame rate kiekvienas laukiantis vadovas gauna po viena
    # vieta, tik tada dalinamos likusios. Taip niekas nelaukia eiles gale, bet ir
    # vietos nestovi tuscios, kai laukiancziu maziau nei vietu.
    raundas = 1
    while laisva > 0 and eiles:
        dabar_vykdo = list(_VYKDOMI.values())
        kandidatai = [k for k in eiles if dabar_vykdo.count(k) < raundas]
        if not kandidatai:
            raundas += 1
            if raundas > LYGIAGRETUMAS:
                return
            continue
        raktas = min(kandidatai, key=lambda k: _EILES_LAIKAS.get(k, 0.0))
        _, vartotojas, vardas, failas, kelias = eiles[raktas].pop(0)
        if not eiles[raktas]:
            del eiles[raktas]

        _EILES_LAIKAS[raktas] = time.time()
        FONO_BUSENA[kelias] = "vyksta"
        _VYKDOMI[kelias] = raktas
        uzd = asyncio.create_task(_fono_darbas(vartotojas, vardas, failas, kelias))
        _UZDUOTYS.add(uzd)
        uzd.add_done_callback(_UZDUOTYS.discard)
        laisva -= 1


def _ne_saskaita_pigiai(baitai: bytes) -> bool:
    """PIGUS filtras PRIES pilna AI skaityma (ilgu sutarciu/uzsakymu atvejis):
    1) 0 tokenu: PDF teksto sluoksnyje nera saskaitos pozymiu (saskaita
       faktura / invoice / faktura...) -> tikrai ne saskaita.
    2) ilgas dokumentas (>=6 psl.) SU pozymiu zodziais (sutartyse jie buna
       mokejimo salygose) — mazas AI klausimas is pirmu puslapiu teksto
       (~0.01 ct, poros sekundziu), o ne visu puslapiu skaitymas.
    Abejones atveju grazina False — sprendzia pilna ekstrakcija kaip iki siol."""
    if not baitai.startswith(b"%PDF"):
        return False
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(baitai))
        psl = len(r.pages)
        t = "".join((p.extract_text() or "") for p in r.pages[:4])
    except Exception:
        return False
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    if len(t.strip()) < 80:
        return False   # skenuota / tuscias tekstas — tegul ziuri AI
    pozymiai = ("saskaita faktura", "saskaita-faktura", "faktura", "invoice", "rechnung", "factuur")
    if not any(p in t for p in pozymiai):
        return True
    if psl < 6 or not ekstrakcija.MODELIS.startswith("gemini"):
        return False
    try:
        ats = ekstrakcija._gemini_post(ekstrakcija.MODELIS, [{"text":
            "Ar sis dokumentas yra PIRKIMO PVM SASKAITA FAKTURA (ne sutartis, ne "
            "uzsakymas, ne pasiulymas, ne vaztarastis)? Dokumento pradzios tekstas:\n\n" + t[:6000]}],
            {"type": "object", "properties": {"saskaita": {"type": "boolean"}},
             "required": ["saskaita"]}, 2000)
        return not json.loads(ekstrakcija._gemini_tekstas(ats)).get("saskaita", True)
    except Exception:
        return False


def _ne_saskaita_i_prieda(apl: "Aplinka", fold: str, failas: str, baitai: bytes, rez: dict) -> bool:
    """PDF, kuri AI atpazino kaip NE saskaita (uzsakymas, pasiulymas, sutartis),
    is laisko tampa PAKETO PRIEDU prie to paties laisko saskaitu — ne atskiru
    saskaitos irasu. Grazina True, jei perkelta (buvo prie ko segti)."""
    saknis = os.path.dirname(apl.pastas)
    kom_kelias = os.path.join(saknis, "komentarai.json")
    try:
        with open(kom_kelias, encoding="utf-8") as f:
            visi = json.load(f)
    except Exception:
        visi = {}
    raktas = f"{fold}/{failas}"
    manoji = visi.get(raktas) or {}
    kartu = [v for v in (manoji.get("kartu") or []) if v != failas]
    if not kartu:
        return False   # laiske nebuvo kitu saskaitu — nera prie ko segti

    # 1) failas i priedai/ (+ OneDrive, jei pavyksta — atsidarys per Office/nuoroda)
    priedu_dir = os.path.join(saknis, "priedai")
    os.makedirs(priedu_dir, exist_ok=True)
    fv = hashlib.sha256(baitai).hexdigest()[:12] + "_" + pastas._saugus_priedo_vardas(failas)
    fk = os.path.join(priedu_dir, fv)
    if not os.path.exists(fk):
        with open(fk, "wb") as f:
            f.write(baitai)
    od_id = ""
    try:
        import httpx as _hx
        _tk, token = pastas._galiojantis_token(apl.pastas)
        with _hx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=60) as kl:
            od_id = pastas._i_onedrive(kl, failas, baitai)
    except Exception:
        pass
    priedas = {"vardas": pastas._saugus_priedo_vardas(failas), "failas": fv, "od_id": od_id}

    # 2) komentarai: prieda gauna VISOS kitos to laisko saskaitos; savo irasa isimam
    atnaujinti = []
    for v in kartu:
        r2 = f"{fold}/{v}"
        irasas = visi.get(r2)
        if irasas is None:
            irasas = visi[r2] = {"nuo": manoji.get("nuo") or "", "tema": manoji.get("tema") or "",
                                 "tekstas": manoji.get("tekstas") or "", "priedai": [],
                                 "paketas": manoji.get("paketas") or "", "kartu": list(kartu)}
        if not any(p.get("failas") == fv for p in irasas.get("priedai") or []):
            irasas.setdefault("priedai", []).append(dict(priedas))
        irasas["kartu"] = [x for x in (irasas.get("kartu") or []) if x != failas]
        atnaujinti.append(r2)
    visi.pop(raktas, None)
    laik = kom_kelias + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump(visi, f, ensure_ascii=False, indent=1)
    os.replace(laik, kom_kelias)

    # 3) saskaitos artefaktai isvalomi: irasas, PDF folderyje, kopija ikelti/
    if rez.get("id"):
        saskaitos.trinti(apl.sask, rez["id"])
    for kel in (os.path.join(apl.darb, fold, failas),
                os.path.join(apl.ikelti, os.path.basename(rez.get("failo_id") or ""))):
        try:
            if kel and os.path.isfile(kel):
                os.remove(kel)
        except OSError:
            pass

    # 4) jau ISSAUGOTOS to laisko saskaitos gauna prieda i savo korteles
    for r2 in atnaujinti:
        f2, v2 = r2.split("/", 1)
        sid = _failo_id(os.path.join(apl.darb, f2, v2))
        irasas = saskaitos.gauti(apl.sask, sid) if sid else None
        if irasas is not None:
            irasas["vadybininko_komentarai"] = visi.get(r2)
            saskaitos.issaugoti(apl.sask, irasas, atnaujinti_laika=False)
    return True


async def _fono_darbas(vartotojas: str, vardas: str, failas: str, kelias: str):
    """Viena saskaita: AI ekstrakcija atskirame sraute, kad kiti nelauktu."""
    pradzia = time.time()
    try:
        with open(kelias, "rb") as fh:
            baitai = fh.read()
        apl = Aplinka(vartotojas)
        # DOKUMENTO TIPO SIETAS pries pilna skaityma (klasifikacija.py): 0 tokenu
        # teksto pozymiai -> mazas klausimas stipresniam modeliui tik is pirmo
        # puslapio (isankstines, pasiulymai, sutartys, nuotraukos) -> tik tikros
        # saskaitos eina i brangu pilna skaityma
        mime_f = MIME.get(os.path.splitext(failas)[1].lower()) or "application/pdf"
        tipas = await asyncio.to_thread(klasifikacija.ivertinti, baitai, mime_f)
        if tipas.get("kaina_ct"):
            try:
                islaidos.prideti(f"{vartotojas} / {vardas} (tipo patikra)", tipas["kaina_ct"])
            except Exception:
                pass
        print(f"[klasifikacija] {vardas}/{failas}: {tipas.get('tipas')} "
              f"({'AI ' + str(tipas.get('modelis') or '') if tipas.get('sluoksnis') else '0 tokenu'}"
              f"{', ~' + str(tipas.get('kaina_ct')) + ' ct' if tipas.get('kaina_ct') else ''})"
              f"{' — ' + tipas['pagrindas'] if tipas.get('pagrindas') else ''}", flush=True)
        if not tipas.get("saskaita"):
            if await asyncio.to_thread(_ne_saskaita_i_prieda, apl, vardas, failas, baitai,
                                       {"dokumento_tipas": tipas.get("tipas")}):
                FONO_BUSENA.pop(kelias, None)
                _FONO_BANDYMAI.pop(kelias, None)
                _PAKARTOTI.pop(kelias, None)
                print(f"[fonas] {vardas}/{failas}: NE saskaita ({tipas.get('tipas')}) -> paketo priedas, pilnas skaitymas nevyko", flush=True)
                return
        rez = await asyncio.to_thread(
            _apdoroti, apl, baitai, failas,
            {"tipas": "folderis", "darbuotojas": vardas, "failas": failas})
        # APSAUGA: MUSU PACIU israsyta saskaita (pardavejas = viena is musu imoniu
        # pagal Pragmos konteksta) negali tapti pirkimu — zymim kaip ne saskaita
        if rez and rez.get("yra_saskaita") and _musu_imone(
                ((rez.get("saskaita") or {}).get("tiekejas") or {}).get("imones_kodas")):
            rez["yra_saskaita"] = False
            rez["dokumento_tipas"] = "musu_pardavimas"
            saskaitos.issaugoti(apl.sask, rez, atnaujinti_laika=False)
            print(f"[fonas] {vardas}/{failas}: pardavejas — MUSU imone -> ne pirkimo saskaita", flush=True)
        # UZSAKYMAS/PASIULYMAS/ISANKSTINE (AI: ne pirkimo saskaita) laiske su
        # saskaitomis -> ne atskiras saskaitos irasas, o PAKETO PRIEDAS
        if rez and not rez.get("yra_saskaita"):
            try:
                if await asyncio.to_thread(_ne_saskaita_i_prieda, apl, vardas, failas, baitai, rez):
                    print(f"[fonas] {vardas}/{failas}: AI sako NE saskaita -> perkelta i paketo priedus", flush=True)
            except Exception as e2:
                print(f"[fonas] ne-saskaitos perkelti nepavyko ({e2}) — lieka kaip irasas", flush=True)
        FONO_BUSENA.pop(kelias, None)
        _FONO_BANDYMAI.pop(kelias, None)
        _PAKARTOTI.pop(kelias, None)
        print(f"[fonas] apdorota: {vartotojas}/{vardas}/{failas} per {time.time() - pradzia:.1f} s", flush=True)
    except Exception as e:
        zinute = getattr(e, "detail", None) or str(e)
        _FONO_BANDYMAI[kelias] = _FONO_BANDYMAI.get(kelias, 0) + 1
        if _FONO_BANDYMAI[kelias] >= 2:
            FONO_BUSENA[kelias] = f"klaida: {zinute}"[:300]
            print(f"[fonas] NEPAVYKO (galutinai) {vartotojas}/{vardas}/{failas}: {zinute}", flush=True)
        else:
            FONO_BUSENA.pop(kelias, None)   # bandysim dar karta, bet ne tuoj pat:
            _PAKARTOTI[kelias] = time.time() + 30   # jei AI buvo apkrauta, duodam atsikvepti
            print(f"[fonas] nepavyko {vartotojas}/{vardas}/{failas}, bandysim vel po 30 s: {zinute}", flush=True)
    finally:
        _VYKDOMI.pop(kelias, None)
        # Vieta atsilaisvino — nelaukiam kito rato, imam kita saskaita is karto.
        if (os.environ.get("AUTO_APDOROTI") or "1") == "1":
            try:
                await _fono_praejimas()
            except Exception as e:
                print(f"[fonas] eiles klaida: {e}", flush=True)


# ── API ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def pagrindinis():
    # no-store: HTML visada sviezias — po diegimo niekas nebemato senos versijos
    # (PDF/priedai kesuojami atskirai pagal hash vardus, jiems tai netrukdo)
    with open(os.path.join(_DIR, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


# ── Prisijungimas (aktyvus tik kai .env yra PRISIJUNGIMO_SLAPTAZODIS) ───────
@app.get("/login")
def prisijungimo_puslapis():
    if not prisijungimas.ijungtas():
        return RedirectResponse("/")
    with open(os.path.join(_DIR, "static", "prisijungimas.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


class PrisijungimoUzklausa(BaseModel):
    vardas: str
    slaptazodis: str


@app.post("/api/prisijungti")
def prisijungti(u: PrisijungimoUzklausa):
    if not prisijungimas.ijungtas():
        return {"pavyko": True}
    vardas = (u.vardas or "").strip()[:40] or "Darbuotojas"
    # PASKYROS: jei vardas turi paskyra — galioja TIK jos slaptazodis;
    # vardai be paskyros jungiasi bendru slaptazodziu (kaip iki siol).
    if paskyros.turi_paskyra(vardas):
        if not paskyros.tikrinti(vardas, u.slaptazodis):
            raise HTTPException(401, "Neteisingas slaptažodis")
        paskyros.pazymeti_prisijungima(vardas)
    elif not prisijungimas.slaptazodis_geras(u.slaptazodis):
        raise HTTPException(401, "Neteisingas slaptažodis")
    atsakas = JSONResponse({"pavyko": True, "vardas": vardas})
    # Be max_age: cookie miršta uždarius naršyklę, o galiojimą (30 min be
    # veiklos) tikrina pats serveris pagal cookie viduje pasirašytą laiką
    atsakas.set_cookie("sesija", prisijungimas.sesijos_cookie(vardas), httponly=True,
                       samesite="lax", secure=prisijungimas.saugus_cookie())
    return atsakas


@app.post("/api/atsijungti")
def atsijungti():
    atsakas = JSONResponse({"pavyko": True})
    atsakas.delete_cookie("sesija")
    return atsakas


# ── Admin panele: paskyru valdymas (/admin) ─────────────────────────────────
# Teises: admin paskyra. Kol paskyru nera NE VIENOS — leidziama visiems
# prisijungusiems (kad butu imanoma susikurti pirmaja; ji tampa admin).
# ── Kas prisijunges: (vardas, ar_administratorius) ──────────────────────────
# MODELIS: darbuotoja mato TIK savo folderi ir savo saskaitas; adminas — viska.
# Lokaliai (be prisijungimo) ir kol nera ne vienos paskyros — viskas matoma.
def _kas(request: Request) -> tuple[str, bool]:
    vardas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    if not prisijungimas.ijungtas() or not paskyros.yra_paskyru():
        return vardas, True
    return vardas, paskyros.ar_admin(vardas)


class Aplinka:
    """Vienos paskyros duomenu keliai. Kitos paskyros duomenu pasiekti neimanoma —
    visi failai skaitomi/rasomi TIK per sitos aplinkos kelius."""

    def __init__(self, vartotojas: str):
        self.vartotojas = vartotojas or "bendra"
        self.darb = aplinka.darbuotojai_dir(self.vartotojas)
        self.sask = aplinka.saskaitos_dir(self.vartotojas)
        self.ikelti = aplinka.ikelti_dir(self.vartotojas)
        self.pastas = aplinka.pastas_failas(self.vartotojas)
        self.istrinti = aplinka.istrinti_failas(self.vartotojas)
        self.atmintis = aplinka.atmintis_failas(self.vartotojas)
        self.komentarai = aplinka.komentarai_failas(self.vartotojas)
        self.priedai = aplinka.priedai_dir(self.vartotojas)
        self.sutartys = aplinka.sutartys_failas(self.vartotojas)
        _pradiniai_folderiai(self.darb)


def _apl(request: Request) -> Aplinka:
    """Prisijungusio vartotojo aplinka (is sesijos slapuko).
    Paskyra su 'aplinka' lauku (vadybininkas) dirba TOJE aplinkoje —
    duomenys tie patys kaip apskaitos, skiriasi tik rodinys ir teises."""
    vardas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    v = vardas or (os.environ.get("APLINKOS_SAVININKAS") or "Savininkas")
    return Aplinka(paskyros.aplinkos_pavadinimas(v) or v)


def _tik_adminui(request: Request) -> None:
    _, adminas = _kas(request)
    if not adminas:
        raise HTTPException(403, "Šį veiksmą gali atlikti tik administratorius")


def _admin_tikrinimas(request: Request) -> str:
    vardas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    if not prisijungimas.ijungtas():
        return vardas or "admin"   # lokalus rezimas be prisijungimo
    if paskyros.yra_paskyru() and not paskyros.ar_admin(vardas):
        raise HTTPException(403, "Reikia administratoriaus teisiu")
    return vardas


@app.get("/admin")
def admin_puslapis(request: Request):
    _admin_tikrinimas(request)
    with open(os.path.join(_DIR, "static", "admin.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/admin/paskyros")
def admin_paskyru_sarasas(request: Request):
    _admin_tikrinimas(request)
    return {"paskyros": paskyros.sarasas(),
            "bendras_veikia": not paskyros.yra_paskyru()}


@app.get("/api/admin/aplinkos")
def admin_aplinkos(request: Request):
    """Adminui — ka turi KIEKVIENA paskyra: projektu vadovu folderius, ju sasakaitas,
    prijungta pasta ir siunteju priskyrimus. TIK perziura (duomenys nemaisomi)."""
    _admin_tikrinimas(request)
    rez = []
    for a in aplinka.visos():
        darb = aplinka.darbuotojai_dir(a)
        folderiai = []
        for f in sorted(os.listdir(darb)) if os.path.isdir(darb) else []:
            kelias = os.path.join(darb, f)
            if not os.path.isdir(kelias):
                continue
            failai = sorted(x for x in os.listdir(kelias) if os.path.isfile(os.path.join(kelias, x)))
            folderiai.append({"vardas": f, "failai": failai})
        sdir = aplinka.saskaitos_dir(a)
        try:
            b = pastas.busena(aplinka.pastas_failas(a))
        except Exception:
            b = {}
        rez.append({
            "aplinka": a,
            "folderiai": folderiai,
            "saskaitu": len([x for x in os.listdir(sdir) if x.endswith(".json")]) if os.path.isdir(sdir) else 0,
            "pastas": ((b.get("paskyra") or {}) or {}).get("email") or "",
            "priskyrimai": b.get("priskyrimai") or {},
        })
    return {"aplinkos": rez}


class AdminPriskyrimas(BaseModel):
    aplinka: str
    adresas: str
    folderis: str = ""


@app.post("/api/admin/pastas/priskyrimai")
def admin_pasto_priskyrimas(u: AdminPriskyrimas, request: Request):
    """Siuntėjų priskyrimai valdomi TIK is admin panelės — bet kuriai aplinkai.
    (Apskaitininkėms priskyrimu keitimas uzdarytas.)"""
    _admin_tikrinimas(request)
    if u.aplinka not in aplinka.visos():
        raise HTTPException(404, "Tokios aplinkos nėra")
    fold = u.folderis.strip()
    darb = aplinka.darbuotojai_dir(u.aplinka)
    if fold and not os.path.isdir(os.path.join(darb, _saugus_folderio_vardas(fold))):
        raise HTTPException(400, f"Folderio „{fold}“ toje aplinkoje nėra")
    try:
        p = pastas.issaugoti_priskyrima(aplinka.pastas_failas(u.aplinka), u.adresas, fold)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[pastas] admin pakeitė priskyrimą ({u.aplinka}): {u.adresas} -> {fold or 'PAŠALINTA'}", flush=True)
    return {"pavyko": True, "priskyrimai": p}


@app.get("/api/admin/islaidos")
def admin_islaidos(request: Request):
    """AI islaidu suvestine pagal vartotoja (tik adminui)."""
    _admin_tikrinimas(request)
    return {"islaidos": islaidos.suvestine(), "modelis": ekstrakcija.MODELIS}


class PaskyrosUzklausa(BaseModel):
    vardas: str
    admin: bool = False
    aplinka: str = ""          # svetimos aplinkos vardas (vadybininkui: "apskaita")
    vadybininkas: bool = False
    folderis: str = ""         # vadybininko savas folderis (pvz. Vadybininkas)


@app.post("/api/admin/paskyros")
def admin_paskyros_kurimas(u: PaskyrosUzklausa, request: Request):
    _admin_tikrinimas(request)
    if u.aplinka and u.aplinka.strip() not in aplinka.visos():
        raise HTTPException(400, f"Aplinkos „{u.aplinka}“ nėra")
    try:
        slaptazodis = paskyros.sukurti(u.vardas, u.admin, u.aplinka, u.vadybininkas, u.folderis)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Nauja paskyra gauna TUSCIA SAVO aplinka (savas pastas, savos saskaitos),
    # NEBENT dirba svetimoje (vadybininkas apskaitos aplinkoje) — tada savos nekuriam
    if not u.aplinka:
        Aplinka(u.vardas.strip())
    return {"pavyko": True, "vardas": u.vardas.strip(), "slaptazodis": slaptazodis}


class PaskyrosVardas(BaseModel):
    vardas: str


class PaskyrosSlaptazodis(BaseModel):
    vardas: str
    slaptazodis: str = ""   # tuscias = sugeneruoti automatiskai


@app.post("/api/admin/paskyros/naujas-slaptazodis")
def admin_naujas_slaptazodis(u: PaskyrosSlaptazodis, request: Request):
    _admin_tikrinimas(request)
    try:
        if u.slaptazodis.strip():
            paskyros.nustatyti_slaptazodi(u.vardas, u.slaptazodis)
            return {"pavyko": True, "vardas": u.vardas, "slaptazodis": u.slaptazodis.strip()}
        return {"pavyko": True, "vardas": u.vardas, "slaptazodis": paskyros.naujas_slaptazodis(u.vardas)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/paskyros/trinti")
def admin_paskyros_trynimas(u: PaskyrosVardas, request: Request):
    vardas = _admin_tikrinimas(request)
    if u.vardas.strip().lower() == (vardas or "").strip().lower():
        raise HTTPException(400, "Savo paties paskyros istrinti negalima")
    try:
        return {"pavyko": paskyros.istrinti(u.vardas)}
    except ValueError as e:
        raise HTTPException(400, str(e))


class PaskyrosAdmin(BaseModel):
    vardas: str
    admin: bool


@app.post("/api/admin/paskyros/admin")
def admin_teisiu_keitimas(u: PaskyrosAdmin, request: Request):
    _admin_tikrinimas(request)
    try:
        paskyros.keisti_admin(u.vardas, u.admin)
        return {"pavyko": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Agento API (Pragmos serverio agentas pasiima XML per HTTPS) ─────────────
# Auth: antraste  X-Agento-Raktas: <AGENTO_RAKTAS is .env>  (tikrina middleware).
@app.get("/api/agentas/eksportas")
def agento_sarasas(visi: int = 0):
    """Sarasas XML failu, kuriu agentas dar NEPAEME (?visi=1 — ir paimti)."""
    return {"failai": eksportas.sarasas(bool(visi))}


@app.get("/api/agentas/laukti")
async def agento_laukimas(sekundes: int = 50):
    """ILGAS LAUKIMAS (long-poll): atsako IS KARTO, kai eileje atsiranda nepaimtu
    failu; jei per `sekundes` neatsirado — grazina tuscia sarasa. Agentas kviecia
    cikle be pauziu -> failas Pragmoje ta pacia sekunde po mygtuko paspaudimo."""
    sekundes = max(1, min(int(sekundes), 55))
    for _ in range(sekundes):
        failai = eksportas.sarasas()
        if failai:
            return {"failai": failai}
        await asyncio.sleep(1)
    return {"failai": []}


@app.get("/api/agentas/eksportas/{failas}")
def agento_failas(failas: str):
    kelias = eksportas.kelias(failas)
    if not kelias:
        raise HTTPException(404, "Failo nera")
    return FileResponse(kelias, media_type="application/xml; charset=utf-8",
                        filename=os.path.basename(kelias))


# Katalogo papildymai is Pragmos. Saugom TIK poras (kodas, pavadinimas), o ne
# vektorius: didelio .npz perrasymas po kiekvieno importo uztruktu ir be reikalo
# kankintu diska. Paleidziant jos pridedamos is naujo — desimciai prekiu tai
# vienas embedding kvietimas.
_PAPILDYMU_FAILAS = os.path.join(_DIR, "katalogo_papildymai.json")


def _irasyti_papildymus(poros: list[tuple[str, str]]) -> None:
    try:
        esami = {}
        if os.path.exists(_PAPILDYMU_FAILAS):
            with open(_PAPILDYMU_FAILAS, encoding="utf-8") as f:
                esami = {k: v for k, v in json.load(f)}
        esami.update({k: v for k, v in poros})
        laik = _PAPILDYMU_FAILAS + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(sorted(esami.items()), f, ensure_ascii=False)
        os.replace(laik, _PAPILDYMU_FAILAS)
    except Exception as e:
        print(f"[katalogas] papildymu issaugoti nepavyko: {e}", flush=True)


def _pritaikyti_papildymus() -> None:
    """Paleidziant: prie .npz katalogo prideda tai, ka Pragma praneše nuo paskutinio
    pilno eksporto. Kai katalogas atnaujinamas is naujo, sitie tiesiog nepakeicia nieko."""
    if KATALOGAS is None or not os.path.exists(_PAPILDYMU_FAILAS):
        return
    try:
        with open(_PAPILDYMU_FAILAS, encoding="utf-8") as f:
            poros = [(k, v) for k, v in json.load(f)]
        if poros:
            rez = KATALOGAS.prideti(poros)
            print(f"[katalogas] papildymai is Pragmos: +{rez['prideta']}, "
                  f"atnaujinta {rez['atnaujinta']} (viso {KATALOGAS.N})", flush=True)
    except Exception as e:
        print(f"[katalogas] papildymu pritaikyti nepavyko: {e}", flush=True)


def _rasti_saskaita(numeris: str):
    """Suranda issaugota saskaita pagal numeri VISOSE aplinkose.
    Grazina (Aplinka, irasas) arba (None, None). Agentas sesijos neturi, todel
    aplinka randama pagal pati dokumenta."""
    nr = _norm_numeris(numeris)
    if not nr:
        return None, None
    for vartotojas in aplinka.visos():
        apl = Aplinka(vartotojas)
        for s in saskaitos.visos(apl.sask):
            if _norm_numeris(s.get("numeris")) == nr:
                return apl, saskaitos.gauti(apl.sask, s["id"])
    return None, None


def _rasti_pagal_uzsakyma(uzsakymo_nr: str) -> list:
    """Visi pirkimai (visose aplinkose), is kuriu surinktas pardavimas su siuo
    UzsakymoNr — [(Aplinka, irasas), ...]."""
    nr = (uzsakymo_nr or "").strip().upper()
    if not nr:
        return []
    rez = []
    for vartotojas in aplinka.visos():
        apl = Aplinka(vartotojas)
        for s in saskaitos.visos(apl.sask):
            if (s.get("uzsakymo_nr") or "").strip().upper() == nr:
                irasas = saskaitos.gauti(apl.sask, s["id"])
                if irasas:
                    rez.append((apl, irasas))
    return rez


def _priskirti_koda_eilutei(numeris: str, eilutes_nr: int, kodas: str, pavadinimas: str) -> bool:
    """Pragmos priskirta PR koda pririsa prie TIEKEJO PREKES (jo kodas / EAN), o
    NE prie pavadinimo — pavadinima Pragmoje gali pakeisti bet kada, o tiekejo
    kodas saskaitoje nekinta. Sriso taskas — eilutes numeris tame paciame XML."""
    apl, irasas = _rasti_saskaita(numeris)
    if not irasas:
        return False
    eil = irasas.get("eilutes") or []
    if not (1 <= eilutes_nr <= len(eil)):
        return False
    e = dict(eil[eilutes_nr - 1])
    e["pirkejo_kodas"] = kodas
    e["pirkejo_pavadinimas"] = pavadinimas or (e.get("pasirinkimas") or {}).get("pavadinimas") or ""
    try:
        atmintis.issaugoti_is_xml(apl.atmintis, {"tiekejas": (irasas.get("saskaita") or {}).get("tiekejas") or {},
                                                "eilutes": [e]})
    except Exception as ex:
        print(f"[katalogas] atminties irasyti nepavyko: {ex}", flush=True)
        return False
    # Pacioje saskaitoje irasom parinkima — atsidarius matysis tikras kodas
    eil[eilutes_nr - 1]["pasirinkimas"] = {"tipas": "kat", "kodas": kodas,
                                           "pavadinimas": e["pirkejo_pavadinimas"]}
    irasas["eilutes"] = eil
    saskaitos.issaugoti(apl.sask, irasas)
    return True


class KatalogoPreke(BaseModel):
    kodas: str
    pavadinimas: str
    eilute: int | None = None      # kurios XML eilutes kortele tai yra (nuo 1)


class KatalogoAtnaujinimas(BaseModel):
    prekes: list[KatalogoPreke]
    saskaitos_numeris: str = ""    # butinas, kad `eilute` turetu prasme


# Katalogo keitimas atmintyje NETURI vykti dviem srautais vienu metu (indeksai
# susimaisytu) — visi prideti() kvietimai eina per viena uzrakta.
_KATALOGO_UZRAKTAS = threading.Lock()


@app.post("/api/agentas/katalogas")
def agento_katalogas(u: KatalogoAtnaujinimas):
    """PRAGMOS IMPORTAS PRANESA, kokias korteles sukure arba rado.

    Butina, nes koda naujai kortelei priskiria PATI Pragma (eiles tvarka pagal
    savo paskutini). Be sito grizimo mes to kodo nezinotume, kita karta ta pacia
    preke vel siustume be kodo — ir importas kurtu dar viena kortele. Cia
    grandine uzsidaro: kortele iskart atsiranda musu kataloge ir tampa randama
    vektorine paieska, o katalogas nustoja senti (nebereikia nesti duombazes).

    Vektorius skaiciuojamas tik naujiems/pasikeitusiems pavadinimams."""
    if KATALOGAS is None:
        raise HTTPException(503, KATALOGO_KLAIDA or "Katalogas neuzkrautas")
    poros = [(p.kodas, p.pavadinimas) for p in u.prekes][:5000]
    if not poros:
        print("[katalogas] is Pragmos: tuscias pranesimas (rysio patikra?)", flush=True)
        return {"prideta": 0, "atnaujinta": 0, "praleista": 0}
    try:
        with _KATALOGO_UZRAKTAS:
            rez = KATALOGAS.prideti(poros)
            if rez["prideta"] or rez["atnaujinta"]:
                _irasyti_papildymus(poros)
    except Exception as e:
        raise HTTPException(502, f"Katalogo papildyti nepavyko: {e}")

    # Eilutes ryšys: prisukam koda prie tiekejo prekes, kad kita karta uzsidetu pats
    susieta = 0
    if u.saskaitos_numeris:
        for p in u.prekes:
            if p.eilute and _priskirti_koda_eilutei(u.saskaitos_numeris, p.eilute,
                                                    p.kodas.strip(), p.pavadinimas.strip()):
                susieta += 1
    rez["susieta"] = susieta
    print(f"[katalogas] is Pragmos: +{rez['prideta']} nauju, {rez['atnaujinta']} atnaujinta, "
          f"{rez['praleista']} nepakito, susieta su eilutemis {susieta} (viso {KATALOGAS.N})", flush=True)
    return rez


class AgentoDokumentai(BaseModel):
    dokumentai: list[dict] = []
    duombaze: str = ""     # paketo baze (agentas siuncia virsuje; tuscia = nustatymu tikroji baze)


@app.post("/api/agentas/kontekstas")
def agento_kontekstas(duomenys: dict):
    """PRAGMOS KONTEKSTAS: is ko rinktis keliant (duombazes, sandeliai, tipai,
    savos imones, pirkejai, projektai, tiekeju numatytieji). Perrasoma pilnai —
    ne suliejama. Atsakome KIEK duombaziu priimta (agentas be skaiciaus
    zymeklio nestumia ir siuncia is naujo)."""
    try:
        priimta = pragma_kontekstas.issaugoti(duomenys)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Konteksto issaugoti nepavyko: {e}")
    print(f"[pragma-kontekstas] priimta {priimta} duombaziu "
          f"(laikas: {duomenys.get('laikas')})", flush=True)
    return {"priimta": priimta}


class ImportoRezultatas(BaseModel):
    failas: str = ""
    duombaze: str = ""
    numeris: str = ""
    data: str = ""
    tiekejas: str = ""
    imones_kodas: str = ""
    busena: str = ""            # ikelta / dublikatas / klaida
    priezastis: str = ""
    pragmos_id: int | None = None
    sutapo_su_id: int | None = None
    dokumentas: str = ""        # pirkimas (numatyta) / pardavimas — bendro XML poroje
    pardavimo_numeris: str = "" # pardavimui: Pragmos suteiktas TKL numeris
    pirkejas: str = ""          # pardavimui: kam parduota


@app.post("/api/agentas/importo-rezultatas")
def agento_importo_rezultatas(u: ImportoRezultatas):
    """GALUTINE issiustos saskaitos busena is Pragmos importo (po viena, is
    karto po bandymo). Registruojama zurnale ir prisegama prie saskaitos —
    UI galetu rodyti 'Pragmoje (ID)' / 'dublikatas' / klaida su priezastimi."""
    irasas = pragma_importai.registruoti(u.dict())
    prisegta = False
    if u.numeris:
        rezultatas = {
            "busena": u.busena, "priezastis": u.priezastis,
            "pragmos_id": u.pragmos_id, "sutapo_su_id": u.sutapo_su_id,
            "duombaze": u.duombaze, "dokumentas": u.dokumentas or "pirkimas",
            "pardavimo_numeris": u.pardavimo_numeris, "pirkejas": u.pirkejas,
            "gauta": irasas["gauta"],
        }
        if (u.dokumentas or "").lower() == "pardavimas":
            # PARDAVIMAS (Document-Sale): numeris = musu UzsakymoNr; rezultatas
            # prisegamas prie VISU pirkimu, is kuriu pardavimas surinktas.
            # Busenos: ikelta / laukia (truksta pirkimo — ne klaida) / dublikatas / klaida
            for apl, sask_irasas in _rasti_pagal_uzsakyma(u.numeris):
                sask_irasas["pragmos_pardavimo_importas"] = rezultatas
                saskaitos.issaugoti(apl.sask, sask_irasas, atnaujinti_laika=False)
                prisegta = True
        else:
            apl, sask_irasas = _rasti_saskaita(u.numeris)
            if sask_irasas:
                sask_irasas["pragmos_importas"] = rezultatas
                saskaitos.issaugoti(apl.sask, sask_irasas, atnaujinti_laika=False)
                prisegta = True
    print(f"[pragma-importas] {u.numeris or u.failas}: {u.busena}"
          f"{' id=' + str(u.pragmos_id) if u.pragmos_id else ''}"
          f"{' (' + u.priezastis + ')' if u.priezastis else ''}"
          f"{' | prisegta prie saskaitos' if prisegta else ''}", flush=True)
    return {"priimta": 1}


class AgentoZinute(BaseModel):
    kam: str
    tekstas: str
    nuo: str = ""


@app.post("/api/agentas/zinutes")
def agento_zinutes_palikimas(u: AgentoZinute):
    """AGENTU SUSIRASINEJIMAS per musu serveri (Pragmos agento pasiulymas —
    jo Claude aplinkoje nera SendMessage). Palieka zinute gavejui."""
    try:
        z = pragma_zinutes.palikti(u.kam, u.tekstas, u.nuo)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[zinutes] {z['nuo'] or '?'} -> {z['kam']}: {z['tekstas'][:80]}", flush=True)
    return {"priimta": 1, "id": z["id"]}


@app.get("/api/agentas/zinutes/istorija")
def agento_zinutes_istorija(kam: str = "pragma", kiek: int = 20):
    """VISOS gavejo zinutes (ir jau paimtos) — NEpazymi. Reikalinga, kai dezute
    skaito du: PowerShell agentas (pazymi) ir Claude sesija (turi matyti istorija)."""
    try:
        return {"zinutes": pragma_zinutes.visos(kam, max(1, min(kiek, 200)))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/agentas/zinutes")
def agento_zinutes_paemimas(kam: str = "pragma"):
    """Atiduoda gavejo NEPAIMTAS zinutes ir pazymi jas paimtomis."""
    try:
        zinutes = pragma_zinutes.paimti(kam)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if zinutes:
        print(f"[zinutes] {kam} pasieme {len(zinutes)} zinute(-es)", flush=True)
    return {"zinutes": zinutes}


@app.post("/api/agentas/dokumentai")
def agento_dokumentai(u: AgentoDokumentai):
    """PRAGMOS DB BUSENU SRAUTAS: agentas siuncia pirkimu/pardavimu dokumentus
    paketais (~300). Issaugom (upsert) ir atsakome KIEK priimta — agentas be
    sio skaiciaus zymeklio nestumia ir ta pati paketa siuncia is naujo,
    tad tuscias 200 cia reikstu tylu duomenu praradima."""
    try:
        priimta = pragma_dokumentai.upsert(u.dokumentai, u.duombaze)
    except Exception as e:
        raise HTTPException(500, f"Dokumentu issaugoti nepavyko: {e}")
    print(f"[pragma-dok] gauta {len(u.dokumentai)}, priimta {priimta} "
          f"(bazeje viso {pragma_dokumentai.kiek()})", flush=True)
    return {"priimta": priimta}


@app.post("/api/agentas/eksportas/{failas}/paimta")
def agento_patvirtinimas(failas: str):
    """Agentas patvirtina, kad faila parsisiunte ir padejo Pragmai — nebesiulyti."""
    if not eksportas.pazymeti_paimta(failas):
        raise HTTPException(404, "Failo nera zurnale")
    return {"pavyko": True}


@app.post("/api/ikelti")
async def ikelti(request: Request, failas: UploadFile = File(...)):
    baitai = await failas.read()
    if len(baitai) > 25 * 1024 * 1024:
        raise HTTPException(400, "Failas per didelis (max 25 MB)")
    # Rankiniai ikelimai gula i folderi „Apskaita" — matosi folderiu tinklelyje,
    # o XML eksportas keliauja i atitinkama Pragmos serverio folderi (apskaita)
    apl = _apl(request)
    vardas = os.path.basename(failas.filename or "saskaita.pdf")
    vardas = re.sub(r'[<>:"/\\|?*]+', "_", vardas).strip() or "saskaita.pdf"
    # VADYBININKO rankinis ikelimas (užsakovo prasymas 09-08): saskaita gula i JO
    # folderi ir lieka JO etape — anksciau nukeliaudavo tiesiai apskaitai
    kas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    vadybininkas = paskyros.ar_vadybininkas(kas)
    folderio_vardas = (paskyros.vadybininko_folderis(kas) if vadybininkas else "") or "Apskaita"
    fold = os.path.join(apl.darb, folderio_vardas)
    os.makedirs(fold, exist_ok=True)
    kelias = os.path.join(fold, vardas)
    if os.path.exists(kelias):
        with open(kelias, "rb") as f:
            if f.read() != baitai:   # kitas turinys tuo paciu vardu — nesumaisyti
                vardas = hashlib.sha256(baitai).hexdigest()[:6] + "_" + vardas
                kelias = os.path.join(fold, vardas)
    if not os.path.exists(kelias):
        with open(kelias, "wb") as f:
            f.write(baitai)
    rez = _apdoroti(apl, baitai, vardas,
                    {"tipas": "ikelta", "darbuotojas": folderio_vardas, "failas": vardas})
    if vadybininkas and not rez.get("is_issaugotos") and rez.get("etapas") != "vadybininkas":
        rez["etapas"] = "vadybininkas"
        saskaitos.issaugoti(apl.sask, rez, atnaujinti_laika=False)
    return rez


@app.get("/api/darbuotojai")
def darbuotojai(request: Request):
    """PROJEKTU VADOVU folderiai su ju saskaitomis (naujausios virsuje).
    Rodomi TIK sios paskyros aplinkos folderiai."""
    apl = _apl(request)
    out = []
    for vardas in sorted(os.listdir(apl.darb)):
        folderis = os.path.join(apl.darb, vardas)
        if not os.path.isdir(folderis):
            continue
        failai = []
        for f in os.listdir(folderis):
            ext = os.path.splitext(f)[1].lower()
            if ext in MIME:
                kelias = os.path.join(folderis, f)
                # failas galejo buti KA TIK istrintas (trynimo mygtukas) — praleisti, ne griuti
                try:
                    st = os.stat(kelias)
                except OSError:
                    continue
                # hash is keso pagal mtime+dydi — nebeskaitom visu PDF kas uzklausa
                sid = _failo_id(kelias)
                if not sid:
                    continue
                issaugota = saskaitos.gauti(apl.sask, sid)
                fono = FONO_BUSENA.get(kelias, "")
                # "Israsyta" = dokumentas jau Pragmos DB (importuotas ar suvestas ranka)
                pragmoje = pragmoje_db = ""
                if issaugota:
                    _s = issaugota.get("saskaita") or {}
                    _dok = pragma_dokumentai.rasti_pirkima(
                        (_s.get("tiekejas") or {}).get("imones_kodas"),
                        _s.get("saskaitos_numeris"))
                    if _dok:
                        pragmoje = _dok.get("busena") or "?"
                        pragmoje_db = _dok.get("duombaze") or pragma_kontekstas.nustatymai()["duombaze"]
                failai.append({
                    "pragmoje_db": pragmoje_db if pragmoje else "",
                    "pavadinimas": f,
                    "dydis_kb": round(st.st_size / 1024, 1),
                    "laikas": st.st_mtime,
                    "apdorota": bool(issaugota),
                    "saskaitos_id": sid if issaugota else None,
                    "xml_sugeneruota": bool(issaugota and issaugota.get("xml_sugeneruota")),
                    "pragmoje": pragmoje,
                    "etapas": (issaugota or {}).get("etapas") or "",
                    "pragmos_importas": (issaugota or {}).get("pragmos_importas") or None,
                    "pragmos_pardavimo_importas": (issaugota or {}).get("pragmos_pardavimo_importas") or None,
                    "uzsakymo_nr": ((issaugota or {}).get("pardavimo_uzsakymas") or {}).get("nr") or "",
                    "vyksta": fono == "vyksta" and not issaugota,
                    "klaida": fono[8:] if fono.startswith("klaida: ") else "",
                })
        # Su XML (baigtos) — folderio apacioje; virsuje tik laukiancios darbo
        failai.sort(key=lambda x: (x["xml_sugeneruota"], -x["laikas"]))
        out.append({"vardas": vardas, "failai": failai,
                    "vadybininko": vardas in _vadybininko_folderiai()})
    return {"darbuotojai": out, "griezta": pastas.griezta_tvarka()}


class DarbuotojoFailas(BaseModel):
    darbuotojas: str
    failas: str


@app.post("/api/darbuotojo-failas")
def darbuotojo_failas(u: DarbuotojoFailas, request: Request):
    """Paspaudus saskaita folderyje — tas pats srautas kaip ikelus ranka.
    Jei jau apdorota (pagal turinio hash) — grazina issaugota BE AI."""
    apl = _apl(request)
    kelias = os.path.join(apl.darb, os.path.basename(u.darbuotojas), os.path.basename(u.failas))
    if not os.path.exists(kelias):
        raise HTTPException(404, f"Failo nera: {u.failas}")
    # Jei si saskaita KAIP TIK skaitoma fone — palaukiam jos ir atiduodam gatava.
    # Kitaip uz ta pati dokumenta sumoketume AI du kartus.
    laukta = 0
    while FONO_BUSENA.get(kelias) == "vyksta" and laukta < 120:
        time.sleep(1)
        laukta += 1
    with open(kelias, "rb") as f:
        baitai = f.read()
    return _apdoroti(apl, baitai, os.path.basename(u.failas),
                     {"tipas": "folderis", "darbuotojas": os.path.basename(u.darbuotojas), "failas": os.path.basename(u.failas)})


# ── PROJEKTU VADOVU folderiu valdymas (kuriami/trinami per UI) ──────────────
# Folderio vardas = projekto vadovas; agentas Pragmos serveryje pagal ji deda XML i
# C:\Saskaitos pajamavimui\<vardas> — todel vardai turi sutapti su tenykščiais.
# Folderiai gyvena TOS PACIOS paskyros aplinkoje.

class DarbuotojoFolderis(BaseModel):
    vardas: str
    trinti_su_failais: bool = False


def _saugus_folderio_vardas(vardas: str) -> str:
    v = re.sub(r'[<>:"/\\|?*]+', "", os.path.basename((vardas or "").strip()))[:40]
    if not v or v.startswith("."):
        raise HTTPException(400, "Blogas folderio vardas")
    return v


@app.post("/api/darbuotojo-folderis")
def folderio_sukurimas(u: DarbuotojoFolderis, request: Request):
    apl = _apl(request)
    v = _saugus_folderio_vardas(u.vardas)
    kelias = os.path.join(apl.darb, v)
    if os.path.isdir(kelias):
        raise HTTPException(400, f"Folderis „{v}“ jau yra")
    os.makedirs(kelias)
    return {"pavyko": True, "vardas": v}


@app.post("/api/darbuotojo-folderis/trinti")
def folderio_trynimas(u: DarbuotojoFolderis, request: Request):
    # TIK ADMINUI: 2026-08-24 apskaitininke istryne folderi su failais, o kartu
    # tyliai nustojo veikti ir siuntejo priskyrimas
    _tik_adminui(request)
    apl = _apl(request)
    v = _saugus_folderio_vardas(u.vardas)
    kelias = os.path.join(apl.darb, v)
    if not os.path.isdir(kelias):
        raise HTTPException(404, f"Folderio „{v}“ nera")
    failai = os.listdir(kelias)
    if failai and not u.trinti_su_failais:
        raise HTTPException(409, f"Folderyje yra {len(failai)} failai(-u). Patvirtink trynima su failais.")
    import shutil
    shutil.rmtree(kelias)
    return {"pavyko": True, "istrinta_failu": len(failai)}


@app.get("/api/saskaitos")
def issaugotu_sarasas(request: Request):
    """Apdorotos saskaitos — TIK sios paskyros aplinkos."""
    sar = saskaitos.visos(_apl(request).sask)
    # Zyme "Pragmoje": dokumentas jau yra Pragmos DB (agento busenu srautas).
    # Reiksme = busena (+ patvirtintas, ? nepatvirtintas, - juodrastis).
    for s in sar:
        dok = pragma_dokumentai.rasti_pirkima(s.get("tiekejo_kodas"), s.get("numeris"))
        if dok:
            s["pragmoje"] = dok.get("busena") or "?"
            # KURIOJE bazeje — ispejimas pries kartotini XML tik tai paciai bazei
            s["pragmoje_db"] = dok.get("duombaze") or pragma_kontekstas.nustatymai()["duombaze"]
    return {"saskaitos": sar}


@app.get("/api/pragma/pasiulymas")
def pragma_pasiulymas(request: Request, tiekejo_kodas: str = "", folderis: str = "", duombaze: str = ""):
    """PRAGMA JUOSTA apskaitos lange: ka siulom siai saskaitai (duombaze,
    sandelis, tipas, statusas, sava imone, grupe) + sarasai select'ams.
    Eiliskumas: apskaitininkes ankstesnis pasirinkimas tiekejui -> Pragmos
    tiekeju istorija -> duombazes numatytieji. `duombaze` — apskaitininkes
    pasirinkta baze: pasiulymas ir sarasai persiskaiciuoja JAI (is musu
    serveryje gulincio konteksto — i Pragma nesikreipiama)."""
    vartotojas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    p = pragma_kontekstas.pasiulymas(tiekejo_kodas, folderis, duombaze, vartotojas)
    n = pragma_kontekstas.nustatymai()
    return {
        "pasiulymas": p,
        "sarasai": pragma_kontekstas.sarasai(p["duombaze"]),
        "auto": bool(n.get("auto")),
        "bandymu_rezimas": bool(n.get("bandymu_rezimas")),
        "kontekstas_yra": pragma_kontekstas.kiek() > 0,
    }


@app.get("/api/pragma/pardavimo-sarasai")
def pragma_pardavimo_sarasai(request: Request, folderis: str = "", duombaze: str = ""):
    """VADYBININKUI ir apskaitai: pirkejai, projektai, pardavimo tipai is Pragmos
    konteksto — pardavimas (kam parduodama) renkamas TIK is siu sarasu. `duombaze`
    — ta pati, i kuria keliamas pirkimas (apskaitininkei ja pakeitus, sarasai
    persikrauna). Kartu — sio vartotojo paskutiniai pasirinkimai toje bazeje."""
    vartotojas = prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")
    return pragma_kontekstas.pardavimo_sarasai(folderis, duombaze, vartotojas)


class PragmaNustatymai(BaseModel):
    auto: bool | None = None
    bandymu_rezimas: bool | None = None
    statusas: str | None = None
    duombaze: str | None = None
    savos_imones: dict | None = None
    auto_folderis: str | None = None   # apskaitininkiu XML i si agento stebima folderi ("apskaita"); tuscias = rankinis kelias


@app.get("/api/admin/pragma-nustatymai")
def admin_pragma_nustatymai(request: Request):
    _admin_tikrinimas(request)
    return pragma_kontekstas.nustatymai()


@app.post("/api/admin/pragma-nustatymai")
def admin_pragma_nustatymu_keitimas(u: PragmaNustatymai, request: Request):
    """GRIZTAMUMO JUNGIKLIAI (tik adminui): auto (XML i 'apskaita' folderi ->
    keliasi pats) / bandymu rezimas (BANDYMAI_* duombaze) / statusas."""
    vardas = _admin_tikrinimas(request)
    pakeitimai = {k: v for k, v in u.dict().items() if v is not None}
    n = pragma_kontekstas.issaugoti_nustatymus(pakeitimai)
    print(f"[pragma] {vardas} pakeitė nustatymus: {pakeitimai}", flush=True)
    return n


@app.get("/api/sutartys")
def sutarciu_sarasas(request: Request):
    """SUTARTINIAI IKAINIAI (pardavimo kainoms) — sios aplinkos sutartys.json.
    Vadybininkas eiluteje parenka pozicija -> pardavimo suma = kiekis x ikainis."""
    return {"sutartys": sutartys.skaityti(_apl(request).sutartys)}


class BusenosIssaugojimas(BaseModel):
    id: str
    saskaita: dict
    eilutes: list


@app.post("/api/saskaita/issaugoti")
def saskaitos_issaugojimas(u: BusenosIssaugojimas, request: Request):
    if not saskaitos.atnaujinti_busena(_apl(request).sask, u.id, u.saskaita, u.eilutes):
        raise HTTPException(404, "Saskaita nerasta")
    return {"pavyko": True}


class PerleidimoUzklausa(BaseModel):
    id: str


class UzemimoUzklausa(BaseModel):
    id: str
    kas: str = ""


@app.post("/api/saskaita/uzimti")
def saskaitos_uzemimas(u: UzemimoUzklausa, request: Request):
    """'Paėmiau' žymė: kad dvi kolegės toje pačioje paskyroje netvarkytų tos
    pačios sąskaitos vienu metu. Grąžina pavyko=False + kas, jei jau užimta."""
    return saskaitos.uzimti(_apl(request).sask, u.id, u.kas)


class PerdavimoUzklausa(BaseModel):
    id: str
    atgal: bool = False


@app.post("/api/saskaita/perduoti")
def saskaitos_perdavimas(u: PerdavimoUzklausa, request: Request):
    """VADYBININKAS perduoda saskaita apskaitai (etapas nuimamas) arba
    atsiima atgal (atgal=True). Pinigu ir eiluciu neliecia."""
    vardas, adminas = _kas(request)
    if not (adminas or paskyros.ar_vadybininkas(vardas)):
        raise HTTPException(403, "Perduoti gali tik vadybininkas arba administratorius")
    apl = _apl(request)
    if not saskaitos.perduoti(apl.sask, u.id, vardas, u.atgal):
        raise HTTPException(404, "Saskaita nerasta")
    # PASKUTINIAI VADYBININKO PASIRINKIMAI (pirkejas, projektas, tipas, baze):
    # perdavimas = patvirtinimas — kita saskaita atsidarys su jais (Pragmos agentas nr.32)
    if not u.atgal:
        try:
            ir = saskaitos.gauti(apl.sask, u.id) or {}
            s = ir.get("saskaita") or {}
            pard = s.get("pardavimas") or {}
            db = ((s.get("pragma") or {}).get("duombaze") or "").strip() \
                or pragma_kontekstas._numatyta_duombaze_vartotojui(pragma_kontekstas.nustatymai(), vardas)
            pragma_kontekstas.isiminti_paskutinius(vardas, db, {
                "pardavimo_tipas": pard.get("tipas"), "pirkejas": pard.get("pirkejas"), "projektas": pard.get("projektas")})
        except Exception as e:
            print(f"[pragma] paskutiniu isiminti nepavyko: {e}")
    print(f"[vadybininkas] {vardas}: {u.id} {'atsiimta' if u.atgal else 'perduota apskaitai'}", flush=True)
    return {"pavyko": True}


class PaketoUzklausa(BaseModel):
    ids: list = []       # >=2 saskaitu id — sujungti i viena paketa
    isardyti: str = ""   # paketo id — isardyti (saskaitos lieka)
    pavadinimas: str = ""


@app.post("/api/saskaita/paketas")
def saskaitu_paketas(u: PaketoUzklausa, request: Request):
    """VADYBININKO rankinis paketavimas: kelios saskaitos i viena 📦 paketa
    (arba paketo isardymas). Saskaitu turinys nesikeicia — tik grupavimas."""
    vardas, adminas = _kas(request)
    if not (adminas or paskyros.ar_vadybininkas(vardas)):
        raise HTTPException(403, "Paketus valdo vadybininkas arba administratorius")
    apl = _apl(request)
    if u.isardyti:
        kiek = saskaitos.isardyti_paketa(apl.sask, u.isardyti)
        print(f"[vadybininkas] {vardas}: paketas {u.isardyti} isardytas ({kiek} sask.)", flush=True)
        return {"pavyko": True, "atnaujinta": kiek}
    if len(u.ids) < 2:
        raise HTTPException(400, "Paketui reikia bent dvieju saskaitu")
    try:
        pid = saskaitos.sujungti_i_paketa(apl.sask, u.ids, (u.pavadinimas or "").strip()[:120])
    except ValueError as e:
        raise HTTPException(404, str(e))
    print(f"[vadybininkas] {vardas}: sujungta i paketa {pid} ({len(u.ids)} sask.)", flush=True)
    return {"pavyko": True, "paketas": pid}


@app.post("/api/saskaita/atlaisvinti")
def saskaitos_atlaisvinimas(u: UzemimoUzklausa, request: Request):
    return {"pavyko": saskaitos.atlaisvinti(_apl(request).sask, u.id, u.kas)}


class FolderioFailoTrynimas(BaseModel):
    darbuotojas: str
    failas: str


@app.post("/api/darbuotojo-failas/trinti")
def darbuotojo_failo_trynimas(u: FolderioFailoTrynimas, request: Request):
    """PILNAS saskaitos trynimas is programos: PDF darbuotojo folderyje +
    issaugotas apdorojimas (saskaitos/{id}.json) + kopija ikelti/.
    ISTRINTA = NEBERA SISTEMOJE: ta pacia saskaita galima atsiusti is naujo
    (senas laiskas pats negrizta — matyti_laiskai ji praleidzia)."""
    apl = _apl(request)
    kelias = os.path.join(apl.darb, os.path.basename(u.darbuotojas), os.path.basename(u.failas))
    if not os.path.isfile(kelias):
        raise HTTPException(404, "Failo nera")
    with open(kelias, "rb") as f:
        baitai = f.read()
    saskaitos_id = hashlib.sha256(baitai).hexdigest()[:12]
    failo_id = saskaitos.trinti(apl.sask, saskaitos_id)   # None, jei apdorojimo nebuvo
    if failo_id:
        try:
            os.remove(os.path.join(apl.ikelti, os.path.basename(failo_id)))
        except OSError:
            pass
    os.remove(kelias)
    FONO_BUSENA.pop(kelias, None)
    _FONO_BANDYMAI.pop(kelias, None)
    return {"pavyko": True, "istrinta_apdorota": bool(failo_id)}


@app.post("/api/saskaita/trinti")
def saskaitos_trynimas(u: PerleidimoUzklausa, request: Request):
    """Pasalina apdorota saskaita (issaugota JSON + PDF kopija ikelti/).
    Originalas darbuotojo folderyje NELIECIAMAS — paspaudus ji, AI apdoros is naujo."""
    apl = _apl(request)
    esama = saskaitos.gauti(apl.sask, u.id) or {}
    failo_id = saskaitos.trinti(apl.sask, u.id)
    if failo_id is None:
        raise HTTPException(404, "Saskaita nerasta")
    try:
        os.remove(os.path.join(apl.ikelti, os.path.basename(failo_id)))
    except OSError:
        pass
    # Kad fono darbininkas TUOJ PAT neapdorotu to paties failo is naujo (AI kainuoja),
    # pazymim ji praleisti. Paspaudus faila rankomis — apdoros kaip visada.
    salt = esama.get("saltinis") or {}
    if salt.get("darbuotojas") and salt.get("failas"):
        FONO_BUSENA[os.path.join(apl.darb, salt["darbuotojas"], salt["failas"])] = "praleisti"
    return {"pavyko": True}


@app.post("/api/saskaita/perleisti")
def saskaitos_perleidimas(u: PerleidimoUzklausa, request: Request):
    """AI is naujo TAM PACIAM failui (pvz. po prompto pakeitimo) — perraso issaugota."""
    apl = _apl(request)
    esama = saskaitos.gauti(apl.sask, u.id)
    if not esama:
        raise HTTPException(404, "Saskaita nerasta")
    kelias = os.path.join(apl.ikelti, os.path.basename(esama.get("failo_id") or ""))
    if not os.path.exists(kelias):
        raise HTTPException(404, "Originalo failo nebera ikelti/ folderyje")
    with open(kelias, "rb") as f:
        baitai = f.read()
    rez = _pilna_ekstrakcija(apl, baitai, esama.get("mime") or "application/pdf", esama["failo_id"])
    rez["id"] = u.id
    rez["saltinis"] = esama.get("saltinis")
    rez["sukurta"] = esama.get("sukurta")
    rez["xml_sugeneruota"] = False
    # komentarai/pasiulymas isgyvena perleidima (jie ne is AI, o is laisko)
    if esama.get("vadybininko_komentarai"):
        rez["vadybininko_komentarai"] = esama["vadybininko_komentarai"]
    # VADYBININKO busena irgi isgyvena: etapas (pas vadybininka / perduota),
    # parinkta sutartis ir zinute apskaitai — tai zmogaus sprendimai, ne AI
    for laukas in ("etapas", "perdave"):
        if esama.get(laukas):
            rez[laukas] = esama[laukas]
    sena_s = esama.get("saskaita") or {}
    for laukas in ("pardavimo_sutartis", "vadybininko_zinute"):
        if sena_s.get(laukas):
            rez.setdefault("saskaita", {})[laukas] = sena_s[laukas]
    saskaitos.issaugoti(apl.sask, rez)
    try:
        pv = (rez.get("saltinis") or {}).get("darbuotojas")
        islaidos.prideti(f"{apl.vartotojas} / {pv}" if pv else apl.vartotojas,
                         (rez.get("ai") or {}).get("kaina_ct"))
    except Exception:
        pass
    return rez


@app.get("/api/saskaita/{saskaitos_id}")
def issaugota_saskaita(saskaitos_id: str, request: Request):
    apl = _apl(request)
    irasas = saskaitos.gauti(apl.sask, saskaitos_id)
    if not irasas:
        raise HTTPException(404, "Nerasta")
    # ❗ "papildyta" zyme nusiima atidarius — zmogus nauja informacija pamate
    if irasas.pop("papildyta", None):
        saskaitos.issaugoti(apl.sask, dict(irasas), atnaujinti_laika=False)
    irasas["is_issaugotos"] = True
    return irasas


class KelioUzklausa(BaseModel):
    kelias: str


@app.post("/api/ikelti-is-kelio")
def ikelti_is_kelio(u: KelioUzklausa, request: Request):
    """Testavimui lokaliai: apdoroja faila tiesiai is disko (be narsykles dialogo)."""
    _tik_adminui(request)
    if not os.path.exists(u.kelias):
        raise HTTPException(404, f"Failo nera: {u.kelias}")
    with open(u.kelias, "rb") as f:
        baitai = f.read()
    return _apdoroti(_apl(request), baitai, os.path.basename(u.kelias))


@app.get("/failas/{failo_id}")
def failas(failo_id: str, request: Request):
    # Failas imamas TIK is prisijungusio vartotojo aplinkos — kitos paskyros
    # saskaitos PDF nepasiekiamas net zinant jo varda.
    kelias = os.path.join(_apl(request).ikelti, os.path.basename(failo_id))
    if not os.path.exists(kelias):
        raise HTTPException(404, "Nerasta")
    ext = os.path.splitext(kelias)[1].lower()
    # Failo vardas = turinio hash, tad turinys niekada nesikeicia — kesuojam
    # amzinai: antra karta atidaryta saskaita krauna PDF be tinklo (0 ms)
    return FileResponse(kelias, media_type=MIME.get(ext, "application/octet-stream"),
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/priedas/{failas}")
def priedas(failas: str, request: Request):
    """Ne-saskaitos laisko priedas (pvz. kainu lentele) — atsisiuntimui prie
    vadybininko komentaru. TIK is savo aplinkos priedai/ folderio."""
    apl = _apl(request)
    vardas = os.path.basename(failas)
    kelias = os.path.join(apl.priedai, vardas)
    if not os.path.isfile(kelias):
        raise HTTPException(404, "Priedo nera")
    # atsisiunciamas ORIGINALIU vardu (be hash priesdelio); vardas su turinio
    # hash — kesuojama amzinai
    return FileResponse(kelias, filename=vardas.split("_", 1)[-1] if "_" in vardas else vardas,
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/priedas/{failas}/office")
def priedo_office(failas: str, request: Request):
    """LAIKINA Office Online nuoroda priedui. OneDrive cia — tik saugykla:
    nuoroda generuojama KIEKVIENAM atidarymui, niekur nesaugoma ir greitai
    baigiasi, tad nuolatiniu share nuorodu nesikaupia. Prieiga — tik per
    programos sesija (sis endpointas)."""
    apl = _apl(request)
    failas = os.path.basename(failas)
    od_id = ""
    try:
        with open(os.path.join(os.path.dirname(apl.pastas), "komentarai.json"), encoding="utf-8") as f:
            visi = json.load(f)
        for irasas in visi.values():
            for p in irasas.get("priedai") or []:
                if p.get("failas") == failas and p.get("od_id"):
                    od_id = p["od_id"]
                    break
            if od_id:
                break
    except Exception:
        pass
    if not od_id:
        raise HTTPException(404, "Priedas neturi OneDrive kopijos")
    url = pastas.laikina_priedo_nuoroda(apl.pastas, od_id)
    if not url:
        raise HTTPException(502, "Nepavyko sugeneruoti laikinos perziuros nuorodos")
    return RedirectResponse(url)


@app.get("/api/priedas/{failas}/perziura")
def priedo_perziura(failas: str, request: Request):
    """Priedo turinys PERZIURAI programos lange — failas i apskaitininkes
    kompiuteri NEsiunciamas. Excel atkuriamas istikimai (zr. priedu_perziura.py)."""
    apl = _apl(request)
    kelias = os.path.join(apl.priedai, os.path.basename(failas))
    if not os.path.isfile(kelias):
        raise HTTPException(404, "Priedo nera")
    return priedu_perziura.perziura(kelias)


class PaieskosUzklausa(BaseModel):
    tekstas: str
    vienetas: str = ""   # saskaitos eilutes matas — sutampancios korteles arciau


@app.post("/api/paieska")
def paieska(u: PaieskosUzklausa):
    if not KATALOGAS:
        raise HTTPException(503, KATALOGO_KLAIDA or "Katalogas neuzkrautas")
    if not u.tekstas.strip():
        return {"rezultatai": []}
    return {"rezultatai": KATALOGAS.ieskoti(u.tekstas.strip(), top_n=30, vienetas=u.vienetas)}


# ── Naujos kortelės kodo pasiūlymas ─────────────────────────────────────────
# Pragma naujus kodus dalina IS EILES, todel kita laisva galima pasiulyti pacia
# is katalogo. Bet PAPRASTAS MAKSIMUMAS NETINKA: kataloge yra rankomis
# "rezervuotu" numeriu ir i kodo laukeli suvestu bruksniniu kodu — jie gerokai
# didesni uz seka. Todel imam didziausia numeri, aplink kuri dar TIRSTAI stovi
# kiti kodai; pavieniai isskirtiniai numeriai praleidziami.
_KODO_PASIULYMAS: dict | None = None


def _kodu_seka() -> dict | None:
    if KATALOGAS is None:
        return None
    grupes: dict[str, list[int]] = {}
    for k in KATALOGAS.KODAI:
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", str(k).strip())
        if m:
            grupes.setdefault(m.group(1), []).append(int(m.group(2)))  # raidziu dydis — kaip kataloge
    if not grupes:
        return None
    priesdelis = max(grupes, key=lambda p: len(grupes[p]))   # gausiausia = tikroji seka
    sk = sorted(grupes[priesdelis])

    # Nuo virsaus zemyn: pirmas numeris, kuris turi bent 10 kaimynu 1000 ribose.
    # Taip praleidziami pavieniai isskirtiniai (rezervuoti, bruksniniai kodai).
    paskutinis = sk[-1]
    for n in reversed(sk):
        if sum(1 for x in sk if n - 1000 <= x <= n) >= 10:
            paskutinis = n
            break
    # Plotis imamas is PACIOS sekos, ne is virsutiniu numeriu — ten kaip tik
    # guli ilgesnes siukles, ir kitas kodas gautu perteklini nuli priekyje.
    return {"priesdelis": priesdelis, "paskutinis": paskutinis,
            "plotis": len(str(paskutinis)), "uzimti": set(sk)}


def _kodai_is_eksporto(priesdelis: str) -> set[int]:
    """Kodai, kuriuos MES patys jau isleidome i XML. Butina: katalogo kopija
    sensta, o naujos korteles Pragmoje atsiranda is karto — be sito pasiulytume
    numeri, kuris jau panaudotas."""
    naudoti: set[int] = set()
    try:
        for f in os.listdir(eksportas.EKSPORTO_DIR):
            if not f.lower().endswith(".xml"):
                continue
            with open(os.path.join(eksportas.EKSPORTO_DIR, f), encoding="utf-8-sig") as fh:
                for kodas in re.findall(r"<BuyerItemCode>([^<]+)</BuyerItemCode>", fh.read()):
                    m = re.fullmatch(rf"(?i){re.escape(priesdelis)}(\d+)", kodas.strip())
                    if m:
                        naudoti.add(int(m.group(1)))
    except OSError:
        pass
    return naudoti


@app.get("/api/kitas-kodas")
def kitas_kodas(nuo: str = ""):
    """Siulomas kitas laisvas prekės kodas. `nuo` — jau panaudotas kodas toje
    pačioje sąskaitoje, kad dvi naujos kortelės negautų to paties numerio."""
    global _KODO_PASIULYMAS
    if _KODO_PASIULYMAS is None:
        _KODO_PASIULYMAS = _kodu_seka() or {}
    s = _KODO_PASIULYMAS
    if not s:
        return {"kodas": "", "priezastis": "Kataloge nerasta kodų sekos"}

    # Katalogas + jau isleisti i XML (skaitoma kiekvieno kvietimo metu — failu
    # nedaug, o pasenes sarasas cia butu tiesiog zalingas)
    isleisti = _kodai_is_eksporto(s["priesdelis"])
    uzimti = s["uzimti"] | isleisti

    # Nuo ko skaiciuoti toliau: sekos virsune arba MUSU isleisti kodai, jei jie
    # jau nuejo auksciau (katalogo kopija sensta). Katalogo isskirtiniu numeriu
    # cia imti NEGALIMA — tarp ju yra rezervuotu ir bruksniniu kodu, ir seka
    # nusoktu i 200000+. Todel skaiciuojam tik nuo TO, ka isleidom patys, ir tik
    # protingame nuotolyje nuo sekos virsunes.
    numeris = max([s["paskutinis"]] +
                  [n for n in isleisti if s["paskutinis"] < n <= s["paskutinis"] + 50000])
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", (nuo or "").strip())
    if m and m.group(1).lower() == s["priesdelis"].lower():
        numeris = max(numeris, int(m.group(2)))
    while True:
        numeris += 1
        if numeris not in uzimti:
            break
    return {"kodas": f"{s['priesdelis']}{numeris:0{s['plotis']}d}"}


def _pardavimo_dokumentas(apl, irasas: dict, duomenys: dict, pragma_p: dict, nust: dict) -> dict:
    """DOCUMENT-SALE (Pragmos agentas nr.14/19, 2026-09-07): pardavimas — ATSKIRAS XML,
    surinktas is vieno ar keliu pirkimu (vadybininko rankinis paketas). Kiekviena
    eilute rodo i pirkimo <InvoiceNumber> + <LineNumber>, todel paketo pardavimas
    formuojamas tik tada, kai VISU jo pirkimu XML jau suformuotas (eiluciu tvarka
    galutine) — formuojant paskutini pirkima. Grazina:
      xml (str | None), uzsakymo_nr, failas, pirkimai, pirkejas,
      ketinamas — pardavimas planuojamas (pirkejas + pardavimo sumos), tada
                  pirkimas PRIVALO buti Patvirtintas,
      laukia — kitu paketo pirkimu numeriai, kuriu XML dar nera,
      nariai — visi paketo irasai (siam irasui uzsakymo zymei irasyti)."""
    kom = irasas.get("vadybininko_komentarai") or {}
    pid = kom.get("paketas") if kom.get("rankinis") else ""
    nariai = [irasas]
    if pid:
        for s in saskaitos.visos(apl.sask):
            if s.get("paketas") == pid and s.get("rankinis_paketas") and s.get("id") != irasas.get("id"):
                n = saskaitos.gauti(apl.sask, s["id"])
                if n:
                    nariai.append(n)

    def _eilutes(n):
        return (duomenys.get("eilutes") or []) if n is irasas else (n.get("eilutes") or [])

    def _saskaita(n):
        return duomenys if n is irasas else (n.get("saskaita") or {})

    # Antraste: pirmo nario, kuriam parinktas pirkejas (paprastai — sio). Siai
    # saskaitai imama IS UZKLAUSOS (pardavimo_antraste) — apskaitininkes ka tik
    # padaryti pataisymai gali buti dar neissisaugoje diske.
    ant = duomenys.get("pardavimo_antraste")
    pard = {}
    for n in nariai:
        if n is irasas and isinstance(ant, dict):
            p = ant
        else:
            p = (n.get("saskaita") or {}).get("pardavimas") or {}
        if p.get("pirkejas"):
            pard = p
            break
    ketinamas = bool(pard.get("pirkejas")) and any(
        e.get("pardavimo_be_pvm") not in (None, "") for n in nariai for e in _eilutes(n))
    rez = {"xml": None, "uzsakymo_nr": "", "failas": "", "pirkimai": [], "pirkejas": "",
           "ketinamas": ketinamas, "laukia": [], "nariai": nariai}
    if not ketinamas:
        return rez
    laukia = [((n.get("saskaita") or {}).get("saskaitos_numeris") or n.get("id") or "?")
              for n in nariai if n is not irasas and not n.get("xml_sugeneruota")]
    if laukia:
        rez["laukia"] = laukia
        return rez
    klaidos = pragma_kontekstas.patikrinti_pardavima(pard, pragma_p.get("duombaze") or "")
    if klaidos:
        raise HTTPException(400, "Pardavimas netinka: " + "; ".join(klaidos))
    eilutes = []
    for n in nariai:
        s = _saskaita(n)
        nr_xml = xml_generavimas.pirkimo_numeris_xml(s.get("saskaitos_numeris"))
        tiek = (s.get("tiekejas") or {}).get("imones_kodas") or ""
        for nr, e in enumerate(_eilutes(n), 1):
            ps = e.get("pardavimo_be_pvm")
            if ps is None or ps == "":
                continue
            kiekis = float(e.get("kiekis") or 0)
            eilutes.append({
                "pirkimo_saskaita": nr_xml, "pirkimo_eil_nr": nr, "pirkimo_tiekejas": tiek,
                "kiekis": kiekis,
                "kaina": round(float(ps) / kiekis, 4) if kiekis else float(ps),
                "suma": round(float(ps), 2), "pvm_proc": e.get("pvm_proc"),
            })
    # UZSAKYMO NR — Pragmos dublikatu raktas. Kartojant XML tai paciai saskaitai
    # numeris NEKEICIAMAS: antras siuntimas Pragmoje tampa "dublikatas", o ne
    # antru pardavimu. Bandymu rezimo numeriai (TEST-) tikram rezimui netinka.
    # Bandymai ar ne — pagal PASIRINKTA baze (apskaitininke gali rinktis tikra
    # baze net esant bandymu rezimui), o ne pagal bendra jungikli. Numeris
    # kartojamas tik tai paciai bazei: ta pati saskaita i BANDYMAI, o paskui i
    # tikra baze gauna NAUJA numeri — ne dublikatas.
    db = (pragma_p.get("duombaze") or "").strip()
    bandymai = db.upper().startswith("BANDYMAI_") or bool((pragma_kontekstas.imone(db) or {}).get("bandomoji"))
    uzs = ""
    for n in nariai:
        pu = n.get("pardavimo_uzsakymas") or {}
        senas = pu.get("nr") or ""
        if senas and (pu.get("duombaze") or "").strip().upper() == db.upper() and senas.startswith("TEST-") == bandymai:
            uzs = senas
            break
    if not uzs:
        uzs = pragma_kontekstas.kitas_uzsakymo_nr(bandymai)
    d = {
        "pragma": {"duombaze": pragma_p.get("duombaze"), "sandelis": pragma_p.get("sandelis"),
                   "tipas": pard.get("tipas"), "sava_imone": pragma_p.get("sava_imone")},
        "uzsakymo_nr": uzs,
        "data": pard.get("data") or duomenys.get("saskaitos_data") or time.strftime("%Y-%m-%d"),
        "apmoketi_iki": pard.get("apmoketi_iki") or "",
        "pirkejas": pard.get("pirkejas"), "projektas": pard.get("projektas") or "",
        # Pastaba Pragmai — vadybininko laukas (vardas, sutartis); "Komentaras
        # apskaitai" lieka vidinis ir i Pragma NEkeliauja
        "pastaba": pard.get("pastaba") or "",
        "eilutes": eilutes,
    }
    rez.update({"xml": xml_generavimas.generuoti_pardavimo_xml(d), "uzsakymo_nr": uzs,
                "failas": f"{uzs}.xml", "pirkejas": pard.get("pirkejas") or "",
                "pirkimai": sorted({e["pirkimo_saskaita"] for e in eilutes})})
    return rez


@app.post("/api/xml")
def xml(duomenys: dict, request: Request):
    apl = _apl(request)
    # Vadybininkas XML neformuoja — jo darbas baigiasi "Perduoti apskaitai"
    if paskyros.ar_vadybininkas(prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")):
        raise HTTPException(403, "XML formuoja apskaita — vadybininkas sąskaitą tik perduoda")
    # Jei sąskaitą šiuo metu tvarko KITAS žmogus — XML negeneruojamas,
    # kad dvi kolegės nepadarytų dvigubo darbo (paskutinė apsauga, UI irgi blokuoja)
    if duomenys.get("saskaitos_id"):
        laiko = saskaitos.kas_tvarko(apl.sask, duomenys["saskaitos_id"])
        kas = saskaitos._norm_kas(duomenys.get("kas")) or "kolegė"
        if laiko and laiko != kas:
            raise HTTPException(409, "Šią sąskaitą šiuo metu tvarko kolegė — XML negeneruojamas, kad darbas nesidubliuotų.")
    # PRAGMA SEKCIJA: auto rezime PRIVALOMA ir tikrinama pries formuojant —
    # nezinoma reiksme blokuojama cia, o ne krenta i Pragmos _klaidos
    nust = pragma_kontekstas.nustatymai()
    auto = bool(nust.get("auto")) and pragma_kontekstas.kiek() > 0
    pragma_p = duomenys.get("pragma") or {}
    if auto:
        klaidos = pragma_kontekstas.patikrinti(pragma_p)
        if klaidos:
            raise HTTPException(400, "Pragma pasirinkimai netinka: " + "; ".join(klaidos))
    elif not pragma_p.get("duombaze"):
        duomenys.pop("pragma", None)   # rankinis rezimas be pasirinkimu — sekcijos nededam
    # PARDAVIMAS = ATSKIRAS <Document-Sale> XML (Pragmos agentas nr.14/19, 2026-09-07):
    # inline <Pardavimas> pirkimo viduje NEBENAUDOJAMAS. Pardavimas surenkamas is
    # vieno ar keliu pirkimu (vadybininko paketas). Su pardavimu pirkimas PRIVALO
    # buti Patvirtintas — Pragma likuti pardavimui tikrina tik patvirtintuose.
    duomenys.pop("pardavimas", None)
    pardavimas = {"xml": None, "ketinamas": False, "laukia": [], "nariai": []}
    if auto and duomenys.get("saskaitos_id"):
        irasas_x = saskaitos.gauti(apl.sask, duomenys["saskaitos_id"]) or {}
        pardavimas = _pardavimo_dokumentas(apl, irasas_x, duomenys, pragma_p, nust)
        if pardavimas["ketinamas"]:
            pragma_p["statusas"] = "Patvirtintas"
            duomenys["pragma"] = pragma_p
    pard_antraste = duomenys.pop("pardavimo_antraste", None) or {}
    turinys = xml_generavimas.generuoti_xml(duomenys)
    # PASKUTINIAI PASIRINKIMAI (per vartotoja ir baze): XML = patvirtinimas, kad
    # sitie pasirinkimai geri — kita saskaita atsidarys su jais (Pragmos agentas nr.32)
    if pragma_p.get("duombaze"):
        try:
            pragma_kontekstas.isiminti_paskutinius(
                prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or ""), pragma_p["duombaze"],
                {**pragma_p, "pardavimo_tipas": pard_antraste.get("tipas") if isinstance(pard_antraste, dict) else "",
                 "pirkejas": pard_antraste.get("pirkejas") if isinstance(pard_antraste, dict) else "",
                 "projektas": pard_antraste.get("projektas") if isinstance(pard_antraste, dict) else ""})
        except Exception as e:
            print(f"[pragma] paskutiniu isiminti nepavyko: {e}")
    # ATMINTIS: XML generavimas = zmogaus patvirtinimas -> isimenami pr parinkimai
    # (i TOS PACIOS paskyros atminti — kitos apskaitininkes jos nemato)
    try:
        atmintis.issaugoti_is_xml(apl.atmintis, duomenys)
    except Exception as e:
        print(f"[atmintis] issaugoti nepavyko: {e}")
    # MOKYMASIS: apskaitininkes sandelio/tipo pasirinkimas siam tiekejui isimenamas
    if pragma_p.get("duombaze"):
        try:
            pragma_kontekstas.ismokti((duomenys.get("tiekejas") or {}).get("imones_kodas"), pragma_p)
        except Exception as e:
            print(f"[pragma] mokymosi irasyti nepavyko: {e}")
    if duomenys.get("saskaitos_id"):
        saskaitos.pazymeti_xml(apl.sask, duomenys["saskaitos_id"])
    nr = re.sub(r"[^\w.]+", "", _norm_numeris(duomenys.get("saskaitos_numeris"))) or "saskaita"
    # Failo vardas TIK ASCII (Ž -> Z): HTTP antrastes lietuvisku raidziu nepriima
    # (latin-1 500 klaida), o Pragmos agentas tokio vardo nesugebedavo parsisiusti
    nr = unicodedata.normalize("NFD", nr).encode("ascii", "ignore").decode() or "saskaita"

    antrastes = {"Content-Disposition": f'attachment; filename="{nr}.xml"'}
    # EKSPORTAS AGENTUI: kopija i eksportas/ — Pragmos serverio agentas pasiims per HTTPS.
    # Eile BENDRA (vienas agentas), o `darbuotojas` = PROJEKTO VADOVO folderis,
    # pagal kuri agentas deda faila i C:\Saskaitos pajamavimui\<vardas>.
    darbuotojas = ""
    if duomenys.get("saskaitos_id"):
        salt = (saskaitos.gauti(apl.sask, duomenys["saskaitos_id"]) or {}).get("saltinis") or {}
        darbuotojas = salt.get("darbuotojas") or ""
    if not darbuotojas:
        darbuotojas = apl.vartotojas
    # AUTO REZIMAS: agentas kelia TIESIAI i Pragma is tu folderiu, kuriuos stebi
    # (vadybininko folderis ir "apskaita" — Pragmos agento susitarimas). Vadybininko
    # folderio XML lieka jo folderyje; VISU KITU (apskaitininkiu) XML eina i
    # nustatymu auto_folderis (pvz. "apskaita"), jei jis nurodytas. Tuscias
    # auto_folderis = apskaitininkiu XML i savo folderius (rankinis importo langas).
    # GRIZTAMUMAS — auto=false arba auto_folderis tuscias.
    if auto and (nust.get("auto_folderis") or "").strip() and darbuotojas not in _vadybininko_folderiai():
        darbuotojas = nust["auto_folderis"].strip()
    # PARDAVIMO XML — PIRMA (Pragmos agentas prasymas: pardavimas Pragmoje palaukia
    # pirkimu; taip patikrinama ir laukimo logika). Uzsakymo numeris irasomas
    # prie VISU paketo saskaitu — pagal ji prisegamas importo rezultatas.
    if pardavimas.get("xml"):
        try:
            eksportas.issaugoti(duomenys.get("saskaitos_id") or "", pardavimas["failas"],
                                pardavimas["xml"], darbuotojas)
            zyma = {"nr": pardavimas["uzsakymo_nr"], "failas": pardavimas["failas"],
                    "sukurta": time.time(), "pirkimai": pardavimas["pirkimai"],
                    "pirkejas": pardavimas["pirkejas"], "duombaze": pragma_p.get("duombaze") or ""}
            for n in pardavimas["nariai"]:
                # is naujo nuskaitom — pazymeti_xml ka tik keite si irasa diske
                n2 = saskaitos.gauti(apl.sask, n.get("id")) or n
                n2["pardavimo_uzsakymas"] = zyma
                n2.pop("pragmos_pardavimo_importas", None)   # naujas siuntimas — senas rezultatas nebegalioja
                saskaitos.issaugoti(apl.sask, n2, atnaujinti_laika=False)
            antrastes["X-Pardavimo-Failas"] = pardavimas["failas"]
            print(f"[xml] pardavimas {pardavimas['uzsakymo_nr']} <- {', '.join(pardavimas['pirkimai'])}", flush=True)
        except Exception as e:
            print(f"[eksportas] pardavimo issaugoti nepavyko: {e}")
    elif pardavimas.get("laukia"):
        lauk = unicodedata.normalize("NFD", ", ".join(pardavimas["laukia"])).encode("ascii", "ignore").decode()
        antrastes["X-Pardavimas-Laukia"] = lauk or "?"
    try:
        eksportas.issaugoti(duomenys.get("saskaitos_id") or "", f"{nr}.xml", turinys, darbuotojas)
    except Exception as e:
        print(f"[eksportas] nepavyko issaugoti: {e}")
    print(f"[xml] {nr}: {'AUTO -> ' + (pragma_p.get('duombaze') or '?') if auto else 'rankinis -> ' + darbuotojas}", flush=True)
    # PRAGMOS IMPORTAS: jei .env nurodytas folderis (pvz. share'as i Pragmos serveri) -
    # XML papildomai padedamas ten, is kur Pragma importuoja. Atsisiuntimas lieka kaip buves.
    pragma_dir = (os.environ.get("PRAGMA_IMPORTO_FOLDERIS") or "").strip()
    if pragma_dir:
        try:
            os.makedirs(pragma_dir, exist_ok=True)
            with open(os.path.join(pragma_dir, f"{nr}.xml"), "w", encoding="utf-8-sig") as f:
                f.write(turinys)
            antrastes["X-Pragma-Eksportas"] = "ok"
        except Exception as e:
            print(f"[pragma] nepavyko nukopijuoti i importo folderi: {e}")
            antrastes["X-Pragma-Eksportas"] = "klaida"

    return Response(
        # BOM (utf-8-sig) — kad Windows programos lietuviskas raides atpazintu teisingai
        content=("\ufeff" + turinys).encode("utf-8"),
        media_type="application/xml; charset=utf-8",
        headers=antrastes,
    )


# ── Darbalaukio programa (PWA: savo langas, ikona, be narsykles juostu) ────
@app.get("/manifest.json")
def manifestas():
    return FileResponse(os.path.join(_DIR, "static", "manifest.json"),
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sw.js")
def service_worker():
    # Service Worker turi buti saknyje, kad valdytu visa programa
    return FileResponse(os.path.join(_DIR, "static", "sw.js"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{failas}")
def statinis(failas: str):
    kelias = os.path.join(_DIR, "static", os.path.basename(failas))
    if not os.path.exists(kelias) or not failas.lower().endswith((".png", ".ico", ".css", ".js")):
        raise HTTPException(404, "Nerasta")
    # Ikonos nesikeicia — kesuojam savaitei; css/js valandai (kad deploy pasimatytu)
    amzius = 604800 if failas.lower().endswith((".png", ".ico")) else 3600
    return FileResponse(kelias, headers={"Cache-Control": f"public, max-age={amzius}"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(_DIR, "static", "ikona-192.png"), media_type="image/png")


@app.get("/atmintis", response_class=HTMLResponse)
def atminties_puslapis():
    with open(os.path.join(_DIR, "static", "atmintis.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/atmintis")
def atminties_sarasas(request: Request):
    return {"irasai": atmintis.visi(_apl(request).atmintis)}


class TrynimoUzklausa(BaseModel):
    id: str


@app.post("/api/atmintis/trinti")
def atminties_trynimas(u: TrynimoUzklausa, request: Request):
    return {"pavyko": atmintis.trinti(_apl(request).atmintis, u.id)}


# ── Pastas (VIENAS apskaitos pastas; skirstymas folderiams pagal siunteja) ──
def _redirect_uri(tiekejas: str) -> str:
    # Serveryje VIESAS_URL=https://... — ta pati adresa reikia irasyti ir
    # Google/Azure OAuth nustatymuose prie Redirect URI.
    bazinis = (os.environ.get("VIESAS_URL") or "").strip().rstrip("/") or "http://localhost:8300"
    return f"{bazinis}/api/pastas/callback/{tiekejas}"


# KIEKVIENA PASKYRA JUNGIA SAVO PASTA — apskaitininke imones apskaitos dezute,
# testuojantis administratorius savo. Duomenys nesimaiso (kiekvienas savo aplinkoj).
@app.get("/api/pastas/prisijungti")
def pasto_prisijungimas(tiekejas: str, request: Request, aplinka_v: str = ""):
    """Pasto OAuth. `aplinka_v` — ADMIN is /admin prijungia pasta KITAI
    paskyrai (pvz. apskaitai), neprisijungdamas prie jos paskyros."""
    _tik_adminui(request)   # pasto nustatymai — tik administratoriui
    if aplinka_v and aplinka_v not in aplinka.visos():
        raise HTTPException(404, "Tokios aplinkos nera")
    pastas_failas = aplinka.pastas_failas(aplinka_v) if aplinka_v else _apl(request).pastas
    if tiekejas not in ("google", "microsoft"):
        raise HTTPException(400, "tiekejas turi buti google arba microsoft")
    if not pastas.paruostas(pastas_failas, tiekejas):
        raise HTTPException(400, f"Nera {tiekejas} raktu .env faile (CLIENT_ID/SECRET)")
    return RedirectResponse(pastas.auth_url(pastas_failas, tiekejas, _redirect_uri(tiekejas), aplinka=aplinka_v))


@app.get("/api/pastas/callback/{tiekejas}")
def pasto_callback(request: Request, tiekejas: str, code: str = "", state: str = "", error: str = ""):
    global PASTO_KLAIDA, _PASTO_LAIKAS
    if error or not code:
        return RedirectResponse(f"/?pastas=klaida&zinute={error or 'be code'}")
    try:
        apl = _apl(request)
        pastas_failas = apl.pastas
        vardas_logui = apl.vartotojas
        # Admin jungė KITAI aplinkai (state.a) — token'ai rasomi i ja
        try:
            kam = (pastas.is_state(state) or {}).get("a") or ""
        except Exception:
            kam = ""
        if kam:
            _tik_adminui(request)
            if kam not in aplinka.visos():
                raise RuntimeError("Tokios aplinkos nera")
            pastas_failas = aplinka.pastas_failas(kam)
            vardas_logui = kam
        email = pastas.apdoroti_callback(pastas_failas, tiekejas, code, _redirect_uri(tiekejas))
        PASTO_KLAIDA = ""   # prijungus is naujo — senas ispejimas nebeaktualus
        _PASTO_LAIKAS = 0.0  # kitas fono ratas tikrins is karto
        print(f"[pastas] {vardas_logui}: prijungtas pastas {email} ({tiekejas})", flush=True)
        if kam:
            return RedirectResponse("/admin")
    except Exception as e:
        print(f"[pastas] callback klaida: {e}")
        return RedirectResponse("/?pastas=klaida")
    return RedirectResponse("/?pastas=prijungta")


@app.post("/api/pastas/tikrinti")
def pasto_tikrinimas(request: Request):
    global PASTO_KLAIDA
    apl = _apl(request)
    dienos = int(os.environ.get("PASTAS_DIENOS") or 30)
    try:
        rez = pastas.parsisiusti_naujus(apl.pastas, apl.darb, dienos)
        PASTO_KLAIDA = ""
        return rez
    except Exception as e:
        PASTO_KLAIDA = str(e)[:300]
        raise HTTPException(502, str(e))


@app.post("/api/pastas/atjungti")
def pasto_atjungimas(request: Request):
    _tik_adminui(request)
    global PASTO_KLAIDA
    PASTO_KLAIDA = ""
    return {"pavyko": pastas.atjungti(_apl(request).pastas)}


class GoogleRaktai(BaseModel):
    client_id: str
    client_secret: str


@app.post("/api/pastas/google-raktai")
def google_raktu_issaugojimas(u: GoogleRaktai, request: Request):
    """Google OAuth raktai ivedami tiesiai per UI (be .env redagavimo)."""
    _tik_adminui(request)
    if not u.client_id.strip().endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "Client ID turi baigtis .apps.googleusercontent.com — patikrink ar nukopijavai teisinga")
    if len(u.client_secret.strip()) < 10:
        raise HTTPException(400, "Client secret per trumpas — patikrink ar nukopijavai teisinga")
    pastas.issaugoti_google_raktus(_apl(request).pastas, u.client_id, u.client_secret)
    return {"pavyko": True}


class PastoDezute(BaseModel):
    adresas: str = ""


@app.post("/api/pastas/dezute")
def pasto_dezute(u: PastoDezute, request: Request):
    """Imones BENDRA (share) dezute: laiskai imami is jos, o ne is prisijungusio
    zmogaus asmenines. Reikia prieigos prie tos dezutes ir Azure Mail.Read.Shared."""
    _tik_adminui(request)
    try:
        d = pastas.issaugoti_dezute(_apl(request).pastas, u.adresas)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"pavyko": True, "dezute": d}


class PastoPriskyrimas(BaseModel):
    adresas: str
    folderis: str = ""


@app.post("/api/pastas/priskyrimai")
def pasto_priskyrimas(u: PastoPriskyrimas, request: Request):
    """Rankinis „siuntejo adresas -> projekto vadovo folderis". Butinas, kai imoneje
    du to paties vardo zmones (j.kvedaras@ ir j.petraitis@ abu „Jonai").
    TIK ADMINUI — 08-24 apskaitininke netycia istryne priskyrima ir 7 saskaitos
    nukrito i Nepriskirta."""
    _tik_adminui(request)
    apl = _apl(request)
    fold = u.folderis.strip()
    if fold and not os.path.isdir(os.path.join(apl.darb, _saugus_folderio_vardas(fold))):
        raise HTTPException(400, f"Folderio „{fold}“ nera — pirma ji sukurk")
    try:
        p = pastas.issaugoti_priskyrima(apl.pastas, u.adresas, fold)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"pavyko": True, "priskyrimai": p}


@app.get("/api/pastas/busena")
def pasto_busena(request: Request):
    b = pastas.busena(_apl(request).pastas)
    b["klaida"] = PASTO_KLAIDA
    b["automatinis"] = (os.environ.get("AUTO_PASTAS") or "1") == "1"
    return b


@app.get("/api/busena")
def busena(request: Request):
    return {
        "katalogas": KATALOGAS.N if KATALOGAS else 0,
        "katalogo_klaida": KATALOGO_KLAIDA,
        "ai_modelis": ekstrakcija.MODELIS,
        "anthropic_raktas": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_raktas": bool(os.environ.get("OPENAI_API_KEY")),
        "prisijungimas": prisijungimas.ijungtas(),
        "vartotojas": prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or ""),
        # admin nuoroda UI: admin paskyra ARBA (dar nera paskyru — bootstrap)
        "admin": (not paskyros.yra_paskyru())
                 or paskyros.ar_admin(prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")),
        # vadybininko rezimas UI: savas sarasas, pardavimo kaina, be XML
        "vadybininkas": paskyros.ar_vadybininkas(
            prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")),
        # vadybininko SAVAS folderis — mato tik jo saskaitas
        "folderis": paskyros.vadybininko_folderis(
            prisijungimas.vardas_is_cookie(request.cookies.get("sesija") or "")),
    }

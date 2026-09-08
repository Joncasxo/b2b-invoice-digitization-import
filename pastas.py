"""
PASTO PRIJUNGIMAS (Gmail + Outlook) — kaip InvoPull SaaS (google.ts / microsoft.ts reference).

Modelis: VIENAS apskaitos pastas visai imonei. Vadybininkai siuncia saskaitas i ji,
o sistema laiskus SKIRSTO I DARBUOTOJU FOLDERIUS pagal SIUNTEJA:
  1) rankinis priskyrimas pastas.json "priskyrimai" ({"petras.k@imone.lt": "Petras"})
  2) pagal varda: petras@pavyzdys.lt -> folderis "Petras" (vardas yra adreso dalyje)
  3) niekas netiko -> folderis "Nepriskirta" (sukuriamas pats)

Token'ai saugomi pastas.json (lokaliai). AI cia nenaudojamas — PDF tik atsiranda
folderyje; apdorojama paspaudus (kaip iprastai).

Env: GOOGLE_CLIENT_ID/SECRET, MICROSOFT_CLIENT_ID/SECRET — BENDRI visoms aplinkoms.
Aplinka gali tureti SAVA Azure programa (pastas.json: microsoft_client_id/secret) —
tada ji virsesne uz bendra. Scope — tik skaitymas.
"""

import base64
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

import httpx

_DIR = os.path.dirname(os.path.abspath(__file__))

# Ne-saskaitu priedai, prisegami prie vadybininko komentaru (pvz. kainu lentele).
# Vaizdeliai (logotipai parasuose) neimami tycia — vien siuksles.
PRIEDU_PLETINIAI = (".xlsx", ".xls", ".csv", ".ods", ".docx", ".doc", ".txt")

# KIEKVIENA PASKYRA JUNGIA SAVO PASTA: kiekviena funkcija gauna tos aplinkos
# pastas.json kelia (zr. aplinka.py). Apskaitininke prijungia imones apskaitos
# dezute, o testuojantis administratorius — savo; duomenys nesimaiso.

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_TOKEN = "https://oauth2.googleapis.com/token"
G_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Mail.Read.Shared — kad galetume skaityti IMONES BENDRA (share) dezute, prie kurios
# prisijungusiam vartotojui duota prieiga. Bendrai dezutei nereikia nei licencijos,
# nei atskiro slaptazodzio — tai pigiausias budas apskaitos pastui.
# Files.ReadWrite — laisku priedai (Excel/Word pasiulymai) keliami i imones
# OneDrive, kad 📎 atsidarytu TIKRAME Office Online, o ne siustusi i kompa
MS_SCOPE = "offline_access Mail.Read Mail.Read.Shared Files.ReadWrite User.Read"
GRAPH = "https://graph.microsoft.com/v1.0"


def _ms_tenant(failas: str) -> str:
    """'common' — bet kokia dezute; tenanto ID arba domenas (imone.lt) — tik tos
    organizacijos. Single tenant Azure programa per 'common' NEVEIKIA (AADSTS50194)."""
    return _cfg(failas).get("microsoft_tenant") or os.environ.get("MICROSOFT_TENANT") or "common"


def _ms_auth_url(failas: str) -> str:
    return f"https://login.microsoftonline.com/{_ms_tenant(failas)}/oauth2/v2.0/authorize"


def _ms_token_url(failas: str) -> str:
    return f"https://login.microsoftonline.com/{_ms_tenant(failas)}/oauth2/v2.0/token"


def _google_id(failas: str) -> str:
    return os.environ.get("GOOGLE_CLIENT_ID") or _cfg(failas).get("google_client_id") or ""


def _google_secret(failas: str) -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET") or _cfg(failas).get("google_client_secret") or ""


def issaugoti_google_raktus(failas: str, client_id: str, client_secret: str) -> None:
    """Raktai ivedami tiesiai per UI (be .env redagavimo) — saugomi pastas.json."""
    cfg = _cfg(failas)
    cfg["google_client_id"] = client_id.strip()
    cfg["google_client_secret"] = client_secret.strip()
    _irasyti(failas, cfg)


def _ms_id(failas: str) -> str:
    """Aplinkos savi raktai VIRSESNI uz bendrus .env — kad viena paskyra galetu
    tureti kita Azure programa (pvz. admino asmenine) nei visos kitos."""
    return _cfg(failas).get("microsoft_client_id") or os.environ.get("MICROSOFT_CLIENT_ID") or ""


def _ms_secret(failas: str) -> str:
    return _cfg(failas).get("microsoft_client_secret") or os.environ.get("MICROSOFT_CLIENT_SECRET") or ""


def issaugoti_microsoft_raktus(failas: str, client_id: str, client_secret: str, tenant: str = "") -> None:
    """Sios aplinkos atskira Azure programa. Tuscia reiksme — grizta prie bendros .env."""
    cfg = _cfg(failas)
    for raktas, reiksme in (("microsoft_client_id", client_id),
                            ("microsoft_client_secret", client_secret),
                            ("microsoft_tenant", tenant)):
        if reiksme.strip():
            cfg[raktas] = reiksme.strip()
        else:
            cfg.pop(raktas, None)
    _irasyti(failas, cfg)


def paruostas(failas: str, tiekejas: str) -> bool:
    if tiekejas == "google":
        return bool(_google_id(failas) and _google_secret(failas))
    return bool(_ms_id(failas) and _ms_secret(failas))


def _cfg(failas: str) -> dict:
    if not os.path.exists(failas):
        return {}
    try:
        with open(failas, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _irasyti(failas: str, d: dict) -> None:
    os.makedirs(os.path.dirname(failas), exist_ok=True)
    with open(failas, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _state(tiekejas: str, aplinka: str = "") -> str:
    # aplinka — kai ADMIN prijungia pasta KITAI paskyrai (pvz. apskaitai) is
    # admin paneles; callback pagal tai zino, i kuria aplinka rasyti token'us
    return base64.urlsafe_b64encode(json.dumps({"t": tiekejas, "a": aplinka}).encode()).decode()


def is_state(state: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(state.encode()).decode())


# ── OAuth ───────────────────────────────────────────────────────────────────
def auth_url(failas: str, tiekejas: str, redirect_uri: str, aplinka: str = "") -> str:
    st = _state(tiekejas, aplinka)
    if tiekejas == "google":
        p = {
            "client_id": _google_id(failas),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",  # kad gautume refresh_token
            # select_account — kad butu galima pasirinkti KITA deztute (keiciant pasta)
            "prompt": "select_account consent",
            "include_granted_scopes": "true",
            "state": st,
        }
        return f"{G_AUTH}?{httpx.QueryParams(p)}"
    p = {
        "client_id": _ms_id(failas),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": MS_SCOPE,
        "prompt": "select_account",  # leidzia pasirinkti KITA deztute (keiciant pasta)
        "state": st,
    }
    return f"{_ms_auth_url(failas)}?{httpx.QueryParams(p)}"


def _token_uzklausa(tiekejas: str, duomenys: dict, failas: str) -> dict:
    url = G_TOKEN if tiekejas == "google" else _ms_token_url(failas)
    r = httpx.post(url, data=duomenys, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{tiekejas} token klaida: {r.status_code} {r.text[:300]}")
    return r.json()


def apdoroti_callback(failas: str, tiekejas: str, code: str, redirect_uri: str) -> str:
    """code -> token'ai -> issaugom VIENA apskaitos paskyra; grazina pasto adresa."""
    duomenys = {"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
    if tiekejas == "google":
        duomenys |= {"client_id": _google_id(failas), "client_secret": _google_secret(failas)}
    else:
        duomenys |= {
            "client_id": _ms_id(failas),
            "client_secret": _ms_secret(failas),
            "scope": MS_SCOPE,
        }
    t = _token_uzklausa(tiekejas, duomenys, failas)

    email = _profilio_adresas(tiekejas, t["access_token"])
    cfg = _cfg(failas)
    cfg["paskyra"] = {
        "tiekejas": tiekejas,
        "email": email,
        "refresh_token": t.get("refresh_token") or "",
        "access_token": t["access_token"],
        "galioja_iki": time.time() + int(t.get("expires_in") or 3600),
    }
    cfg.setdefault("priskyrimai", {})
    _irasyti(failas, cfg)
    return email or "prijungta"


def _profilio_adresas(tiekejas: str, token: str) -> str | None:
    try:
        h = {"Authorization": f"Bearer {token}"}
        if tiekejas == "google":
            r = httpx.get(f"{G_API}/profile", headers=h, timeout=30)
            return r.json().get("emailAddress") if r.status_code == 200 else None
        r = httpx.get(f"{GRAPH}/me", headers=h, timeout=30)
        if r.status_code != 200:
            return None
        j = r.json()
        return j.get("mail") or j.get("userPrincipalName")
    except Exception:
        return None


def _galiojantis_token(failas: str) -> tuple[str, str]:
    """(tiekejas, access_token); atnaujina per refresh_token jei pasibaiges."""
    cfg = _cfg(failas)
    acc = cfg.get("paskyra")
    if not acc:
        raise RuntimeError("Apskaitos pastas neprijungtas")
    if acc.get("access_token") and time.time() < float(acc.get("galioja_iki") or 0) - 60:
        return acc["tiekejas"], acc["access_token"]
    if not acc.get("refresh_token"):
        raise RuntimeError("Nera refresh_token — prijunk pasta is naujo")

    tiekejas = acc["tiekejas"]
    duomenys = {"refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}
    if tiekejas == "google":
        duomenys |= {"client_id": _google_id(failas), "client_secret": _google_secret(failas)}
    else:
        duomenys |= {
            "client_id": _ms_id(failas),
            "client_secret": _ms_secret(failas),
            "scope": MS_SCOPE,
        }
    t = _token_uzklausa(tiekejas, duomenys, failas)
    acc["access_token"] = t["access_token"]
    acc["galioja_iki"] = time.time() + int(t.get("expires_in") or 3600)
    if t.get("refresh_token"):  # Microsoft rotacija — issaugom nauja
        acc["refresh_token"] = t["refresh_token"]
    _irasyti(failas, cfg)
    return tiekejas, acc["access_token"]


# ── Skirstymas pagal siunteja ───────────────────────────────────────────────
def _be_diakritikos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def _adresas_is(v: str) -> str:
    """'Petras K <petras@x.lt>' -> 'petras@x.lt'"""
    m = re.search(r"<([^>]+)>", v or "")
    return (m.group(1) if m else (v or "")).strip().lower()


# Vadybininko komentaruose paliekama tik zmogaus zinute — parasas ir cituota
# istorija nukerpami pagal UNIVERSALIUS pasto zymeklius (ne imoniu sarasus).
_PARASO_PRADZIOS = ("pagarbiai", "kind regards", "best regards", "su pagarba", "regards,")
_CITATU_PRADZIOS = ("-----original message", "-----persiųst", "-----persiust",
                    "from:", "nuo:", "sent:", "išsiųsta:", "issiusta:")


def _be_paraso(t: str) -> str:
    eilutes = (t or "").splitlines()
    for i, e in enumerate(eilutes):
        s = e.strip().lower()
        if not s:
            continue
        vien_bruksniai = len(s) >= 2 and set(s) <= {"-", "_", "=", " "}
        if vien_bruksniai or s.startswith(_PARASO_PRADZIOS) or s.startswith(_CITATU_PRADZIOS):
            eilutes = eilutes[:i]
            break
    return "\n".join(e.rstrip() for e in eilutes).strip()


def _ms_dezute(failas: str) -> str:
    """Kuria dezute skaitom. Tuscia — prisijungusio zmogaus savo (/me).
    Nurodyta — imones BENDRA (share) dezute; reikia, kad prisijungusiam butu duota
    prieiga prie jos (Full Access) ir Azure leidimo Mail.Read.Shared."""
    return (_cfg(failas).get("dezute") or "").strip()


def _ms_kelias(failas: str) -> str:
    d = _ms_dezute(failas)
    return f"{GRAPH}/users/{d}" if d else f"{GRAPH}/me"


def issaugoti_dezute(failas: str, adresas: str) -> str:
    """Bendra dezute, is kurios imami laiskai. Tuscia = savo dezute."""
    adresas = _adresas_is(adresas) if adresas.strip() else ""
    if adresas and "@" not in adresas:
        raise ValueError("Reikia el. pasto adreso")
    cfg = _cfg(failas)
    if adresas:
        cfg["dezute"] = adresas
    else:
        cfg.pop("dezute", None)
    _irasyti(failas, cfg)
    return adresas


def issaugoti_priskyrima(failas: str, adresas: str, folderis: str) -> dict:
    """Rankinis „adresas -> folderis". Tuscias folderis = priskyrimas salinamas.
    Butinas, kai imoneje du to paties vardo zmones — vardo atpazinimas tada klysta."""
    adresas = _adresas_is(adresas)
    if not adresas or "@" not in adresas:
        raise ValueError("Reikia el. pasto adreso")
    cfg = _cfg(failas)
    p = cfg.get("priskyrimai") or {}
    if folderis.strip():
        p[adresas] = folderis.strip()
    else:
        p.pop(adresas, None)
    cfg["priskyrimai"] = p
    _irasyti(failas, cfg)
    return p


def griezta_tvarka() -> bool:
    """GRIEZTA TVARKA (.env TIK_PRISKIRTI_SIUNTEJAI=1): i vadovu folderius patenka
    TIK is rankomis priskirtu adresu atejusios saskaitos. Visos kitos — i
    „Nepriskirta", ir AI ju neskaito (zr. server._laukiancios).
    Priezastis: bendra imones dezute gauna VISU siunteju saskaitas, o be sitos
    taisykles svetimos nukristu i vienintelio vadovo folderi ir butu apmokestintos."""
    return (os.environ.get("TIK_PRISKIRTI_SIUNTEJAI") or "0") == "1"


def folderis_pagal_tema(tema: str, folderiai: list[str]) -> str:
    """ZYME TEMOJE: laiskas, kurio tema prasideda „[Folderis] ...", eina i ta
    folderi (Power Automate persiuncia is darbuotoju pastu — siuntejas tada
    nebe darbuotojas, o zyme pasako, kam sąskaita). Tik EGZISTUOJANTIS
    folderis; kitaip — '' ir galioja siuntejo taisykles."""
    m = re.match(r"^\s*\[([^\]]{1,40})\]", tema or "")
    if not m:
        return ""
    zyme = _be_diakritikos(m.group(1).strip().lower())
    for f in folderiai:
        if _be_diakritikos(f.lower()) == zyme:
            return f
    return ""


def folderis_pagal_siunteja(siuntejas: str, folderiai: list[str], priskyrimai: dict) -> str:
    """1) rankinis priskyrimas, 2) vardas adreso dalyje (petras@... -> Petras),
    3) jei darbuotojo folderis VIENAS — viskas jam, 4) Nepriskirta.
    Grieztoje tvarkoje galioja TIK 1 punktas — visa kita i „Nepriskirta"."""
    adresas = _adresas_is(siuntejas)
    for em, fold in (priskyrimai or {}).items():
        # Folderio egzistavimo NETIKRINAM: jei kas nors folderi istryne,
        # priskyrimas vis tiek galioja — folderis sukuriamas is naujo issaugant
        # (2026-08-24 istrynus folderi 83 saskaitos tyliai nukrito i Nepriskirta).
        if em.strip().lower() == adresas:
            return fold
    if griezta_tvarka():
        return "Nepriskirta"
    vietine = _be_diakritikos(adresas.split("@")[0])
    geriausias = ""
    for f in folderiai:
        vardas = _be_diakritikos(f.lower())
        if vardas and vardas in vietine and len(vardas) > len(geriausias):
            geriausias = f
    if geriausias:
        return geriausias
    # Kai darbuotojo folderis vienintelis — nera ka skirstyti, viskas jam
    realus = [f for f in folderiai if f != "Nepriskirta"]
    if len(realus) == 1:
        return realus[0]
    return "Nepriskirta"


def _saugus_priedo_vardas(v: str) -> str:
    v = os.path.basename(v or "priedas")
    return re.sub(r'[<>:"/\\|?*]+', "_", v).strip() or "priedas"


def _i_onedrive(kl, vardas: str, baitai: bytes) -> str:
    """Prieda ikelia i OneDrive KAIP SAUGYKLA ('Uzpajamavimo priedai/menuo')
    ir grazina failo ID. Jokiu nuolatiniu share nuorodu NEkuriama — perziurai
    programa kiekvienam atidarymui pasigamina LAIKINA nuoroda
    (laikina_priedo_nuoroda), kuri niekur nesaugoma ir greitai baigiasi.
    Nepavykus grazina '' — UI rodys vietine perziura."""
    try:
        hh = hashlib.sha256(baitai).hexdigest()[:10]
        # Menesiu lentynos — folderis nesisiukslina, sena menesi galima tiesiog istrinti
        menuo = datetime.now().strftime("%Y-%m")
        kelias = f"Uzpajamavimo priedai/{menuo}/{hh}_{_saugus_priedo_vardas(vardas)}"
        r = kl.put(f"{GRAPH}/me/drive/root:/{kelias}:/content", content=baitai,
                   headers={"Content-Type": "application/octet-stream"})
        if r.status_code in (200, 201):
            return (r.json() or {}).get("id") or ""
    except Exception:
        pass
    return ""


def laikina_priedo_nuoroda(failas: str, od_id: str) -> str:
    """LAIKINA Office Online perziuros nuoroda OneDrive failui. Generuojama
    kiekvienam paspaudimui, niekur nesaugoma, galioja trumpai — OneDrive lieka
    tik saugykla, be jokiu nuolatiniu share nuorodu."""
    try:
        tiekejas, token = _galiojantis_token(failas)
        r = httpx.post(f"{GRAPH}/me/drive/items/{od_id}/preview",
                       headers={"Authorization": f"Bearer {token}"}, json={}, timeout=30)
        if r.status_code in (200, 201):
            return (r.json() or {}).get("getUrl") or ""
    except Exception:
        pass
    return ""


def _irasyti_komentarus(failas: str, laisku_info: dict, issaugoti_failai: dict) -> list[str]:
    """VADYBININKO KOMENTARAI: laisko tema+tekstas ir ne-saskaitu priedai
    prisegami prie to laisko ISSAUGOTU PDF (raktas "folderis/failas").
    0 AI — tekstas tik parodomas zmogui prie saskaitos; i XML nekeliauja.
    Priedu failai dedami i aplinkos priedai/ (vardas su turinio hash — be dubliu)."""
    aplinkos_dir = os.path.dirname(failas)
    kelias = os.path.join(aplinkos_dir, "komentarai.json")
    priedu_dir = os.path.join(aplinkos_dir, "priedai")
    try:
        with open(kelias, encoding="utf-8") as f:
            visi = json.load(f)
    except Exception:
        visi = {}
    keista = False
    atnaujinti: list[str] = []
    for nr, pdf_failai in (issaugoti_failai or {}).items():
        info = laisku_info.get(nr) or {}
        # PAKETAS: kelios vieno laisko saskaitos (ar saskaita+pasiulymas) UI
        # atsidaro viename lange kortelemis; XML kiekvienai lieka atskiras
        paketas = ""
        if len(pdf_failai) > 1 or info.get("kiti"):
            paketas = hashlib.sha256(
                ("|".join(sorted(v for _, v in pdf_failai)) + str(int(time.time()))).encode()
            ).hexdigest()[:10]
        if not (info.get("tekstas") or info.get("tema") or info.get("kiti") or paketas):
            continue
        pried_meta = []
        for vardas_p, baitai_p, *likutis in info.get("kiti") or []:
            od_id = likutis[0] if likutis else ""
            os.makedirs(priedu_dir, exist_ok=True)
            fv = hashlib.sha256(baitai_p).hexdigest()[:12] + "_" + _saugus_priedo_vardas(vardas_p)
            fk = os.path.join(priedu_dir, fv)
            if not os.path.exists(fk):
                with open(fk, "wb") as f:
                    f.write(baitai_p)
            pried_meta.append({"vardas": _saugus_priedo_vardas(vardas_p), "failas": fv,
                               "od_id": od_id})
        for fold, v in pdf_failai:
            raktas = f"{fold}/{v}"
            naujas = {"nuo": info.get("nuo") or "", "tema": info.get("tema") or "",
                      "tekstas": info.get("tekstas") or "", "priedai": pried_meta,
                      "paketas": paketas, "kartu": [x for _, x in pdf_failai]}
            senas = visi.get(raktas)
            if senas:
                # PAPILDYMAS, ne perrasymas: priedai jungiami, tekstai lipdomi
                turimi = {p.get("failas") for p in senas.get("priedai") or []}
                naujas["priedai"] = (senas.get("priedai") or []) + \
                    [p for p in pried_meta if p.get("failas") not in turimi]
                st, nt = (senas.get("tekstas") or "").strip(), (naujas.get("tekstas") or "").strip()
                if st and nt and nt not in st:
                    naujas["tekstas"] = st + "\n— — —\n" + nt
                else:
                    naujas["tekstas"] = st or nt
                if not naujas["tema"]:
                    naujas["tema"] = senas.get("tema") or ""
                if senas.get("paketas"):
                    naujas["paketas"] = senas["paketas"]
                    naujas["kartu"] = sorted(set(senas.get("kartu") or []) | set(naujas.get("kartu") or []))
                if naujas == senas:
                    continue   # nieko naujo — zymes nekeliam
            visi[raktas] = naujas
            atnaujinti.append(raktas)
            keista = True
    if keista:
        laik = kelias + ".tmp"
        with open(laik, "w", encoding="utf-8") as f:
            json.dump(visi, f, ensure_ascii=False, indent=1)
        os.replace(laik, kelias)
    return atnaujinti


def _saugus_vardas(v: str) -> str:
    v = os.path.basename(v or "saskaita.pdf")
    v = re.sub(r'[<>:"/\\|?*]+', "_", v).strip() or "saskaita.pdf"
    return v if v.lower().endswith(".pdf") else v + ".pdf"


def parsisiusti_naujus(failas: str, darb_dir: str, dienos: int = 30) -> dict:
    """Laiskai is VIENO apskaitos pasto -> PDF priedai i darbuotoju folderius pagal siunteja.
    Dedup pagal turinio hash per VISUS folderius. 0 AI tokenu."""
    tiekejas, token = _galiojantis_token(failas)
    h = {"Authorization": f"Bearer {token}"}
    cfg_visas = _cfg(failas)
    priskyrimai = cfg_visas.get("priskyrimai") or {}
    # INKREMENTINIS PASTAS: jau apdorotu LAISKU id įsimenami — kitame cikle ju
    # priedai nebesiunciami is naujo (anksciau kas cikla siusdavosi VISI 30 d.
    # priedai vien palyginimui; su 80 PDF laisku tai uztrukdavo minutes).
    matyti_laiskai: dict = dict(cfg_visas.get("matyti_laiskai") or {})
    nauji_matyti: list[str] = []

    folderiai = sorted([f for f in os.listdir(darb_dir) if os.path.isdir(os.path.join(darb_dir, f))])

    # hash -> (folderis, failo vardas): dubliu atpazinimui IR papildymui —
    # tas pats PDF atejes DAR KARTA su nauju tekstu/priedais papildo esama
    esami: dict[str, tuple[str, str]] = {}
    for fold in folderiai:
        for f in os.listdir(os.path.join(darb_dir, fold)):
            kelias = os.path.join(darb_dir, fold, f)
            if os.path.isfile(kelias):
                with open(kelias, "rb") as fh:
                    esami[hashlib.sha256(fh.read()).hexdigest()] = (fold, f)

    # ISTRINTA = NEBERA SISTEMOJE (užsakovo taisykle 2026-08-28): istrinta saskaita
    # gali buti atsiusta is naujo. Senas laiskas pats negrizta ir be blokavimo —
    # matyti_laiskai ji praleidzia; grizta tik SAMONINGAI persiustas naujas laiskas.

    # (siuntejas, failo vardas, baitai, laisko_nr)
    priedai: list[tuple[str, str, bytes, int]] = []
    # laisko_nr -> {nuo, tema, tekstas, kiti:[(vardas, baitai)]} — VADYBININKO
    # KOMENTARAI: laisko tekstas + ne-saskaitu priedai (pvz. kainu lentele).
    # 0 AI — tekstas tiesiog parodomas zmogui prie saskaitos, i XML nekeliauja.
    laisku_info: dict[int, dict] = {}
    laiskai = 0
    with httpx.Client(headers=h, timeout=60) as kl:
        if tiekejas == "google":
            # Be filename:pdf — priedas gali ateiti be pletinio varde (ATT00001, noname).
            # in:anywhere — kad matytume ir Spam (saskaitos is nepazistamu siunteju ten krenta).
            # PDF atsirenkamas zemiau pagal mimeType ir pacius baitus (%PDF).
            q = f"has:attachment newer_than:{dienos}d in:anywhere -in:trash"
            r = kl.get(f"{G_API}/messages", params={"q": q, "maxResults": 25})
            if r.status_code != 200:
                raise RuntimeError(f"Gmail klaida: {r.status_code} {r.text[:200]}")
            zinutes = r.json().get("messages") or []
            laiskai = len(zinutes)
            for laisko_nr, m in enumerate(zinutes):
                if m["id"] in matyti_laiskai:
                    continue
                mr = kl.get(f"{G_API}/messages/{m['id']}", params={"format": "full"})
                if mr.status_code != 200:
                    continue
                mj = mr.json()
                antrastes = (mj.get("payload") or {}).get("headers") or []
                nuo = next((a.get("value") for a in antrastes if (a.get("name") or "").lower() == "from"), "")
                tema = next((a.get("value") for a in antrastes if (a.get("name") or "").lower() == "subject"), "")
                dalys: list[dict] = []
                kiti: list[dict] = []
                teksto_dalys: list[str] = []

                def surinkti(p):
                    vardas_p = p.get("filename") or ""
                    att = (p.get("body") or {}).get("attachmentId")
                    yra_pdf = "pdf" in (p.get("mimeType") or "").lower() or vardas_p.lower().endswith(".pdf")
                    if att and yra_pdf:
                        dalys.append({"vardas": vardas_p or "saskaita.pdf", "id": att})
                    elif att and vardas_p.lower().endswith(PRIEDU_PLETINIAI):
                        kiti.append({"vardas": vardas_p, "id": att})
                    elif not att and (p.get("mimeType") or "").lower() == "text/plain" and (p.get("body") or {}).get("data"):
                        teksto_dalys.append(p["body"]["data"])
                    for pp in p.get("parts") or []:
                        surinkti(pp)

                surinkti(mj.get("payload") or {})
                tekstas = ""
                for t in teksto_dalys:
                    t = t.replace("-", "+").replace("_", "/")
                    try:
                        tekstas += base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")
                    except Exception:
                        pass
                laisku_info[laisko_nr] = {"nuo": _adresas_is(nuo), "tema": tema,
                                          "tekstas": _be_paraso(tekstas or mj.get("snippet") or "")[:4000],
                                          "kiti": []}
                nepavyko_priedu = False
                for d in dalys:
                    ar = kl.get(f"{G_API}/messages/{m['id']}/attachments/{d['id']}")
                    if ar.status_code == 200:
                        data = (ar.json().get("data") or "").replace("-", "+").replace("_", "/")
                        priedai.append((nuo, d["vardas"], base64.b64decode(data + "=" * (-len(data) % 4)), laisko_nr))
                    else:
                        nepavyko_priedu = True
                for d in kiti:
                    ar = kl.get(f"{G_API}/messages/{m['id']}/attachments/{d['id']}")
                    if ar.status_code == 200:
                        data = (ar.json().get("data") or "").replace("-", "+").replace("_", "/")
                        # Gmail keliu OneDrive nuorodos nera (kitas debesis) — liks perziura
                        laisku_info[laisko_nr]["kiti"].append((d["vardas"], base64.b64decode(data + "=" * (-len(data) % 4)), ""))
                    else:
                        nepavyko_priedu = True
                # "Matytas" TIK kai visi priedai parsisiusti — kitaip laiskas
                # liktu praleistas amzinai del vienkartinio tinklo trukio
                if not nepavyko_priedu:
                    nauji_matyti.append(m["id"])
        else:
            nuo_datos = (datetime.utcnow() - timedelta(days=dienos)).strftime("%Y-%m-%dT%H:%M:%SZ")
            filtras = f"receivedDateTime ge {nuo_datos} and hasAttachments eq true"
            # $orderby butinas: be jo Graph negarantuoja, kad $top grazins NAUJAUSIUS laiskus
            # Bendra (share) dezute — /users/<adresas>; savo — /me
            bazinis = _ms_kelias(failas)
            r = kl.get(f"{bazinis}/messages", params={
                "$filter": filtras,
                "$orderby": "receivedDateTime desc",
                "$top": "50",
                "$select": "id,from,subject,receivedDateTime,bodyPreview,body",
            })
            if r.status_code != 200:
                raise RuntimeError(f"Graph klaida: {r.status_code} {r.text[:200]}")
            zinutes = r.json().get("value") or []
            laiskai = len(zinutes)
            for laisko_nr, m in enumerate(zinutes):
                if m["id"] in matyti_laiskai:
                    continue
                nuo = ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
                # HTML -> tekstas ISLAIKANT eilutes (kad parasas atsidurtu savo
                # eiluteje ir ji butu galima nukirpti); style/script — lauk
                kunas = ((m.get("body") or {}).get("content") or "")
                kunas = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", kunas)
                kunas = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6])[^>]*>", "\n", kunas)
                tekstas = html.unescape(re.sub(r"<[^>]+>", " ", kunas))
                tekstas = "\n".join(re.sub(r"[ \t\xa0]+", " ", e).strip() for e in tekstas.splitlines())
                tekstas = re.sub(r"\n{3,}", "\n\n", tekstas).strip()
                # fallback bodyPreview IRGI valomas; jei po valymo tuscia —
                # komentaras tuscias (parasas ne komentaras)
                tekstas = _be_paraso(tekstas) or _be_paraso(m.get("bodyPreview") or "")
                laisku_info[laisko_nr] = {"nuo": _adresas_is(nuo), "tema": m.get("subject") or "",
                                          "tekstas": tekstas[:4000], "kiti": []}
                ar = kl.get(f"{bazinis}/messages/{m['id']}/attachments", params={"$select": "id,name,contentType,size,isInline"})
                if ar.status_code != 200:
                    continue
                nepavyko_priedu = False
                for a in ar.json().get("value") or []:
                    vardas_a = a.get("name") or ""
                    ct = (a.get("contentType") or "").lower()
                    yra_pdf = "pdf" in ct or vardas_a.lower().endswith(".pdf")
                    # NUOTRAUKA kaip dokumentas (telefonu fotografuota saskaita): tik ne
                    # inline (parasu logotipai) ir ne mazyte (< 60 KB — ikonos, parasai)
                    yra_nuotrauka = (ct.startswith("image/") or vardas_a.lower().endswith((".jpg", ".jpeg", ".png"))) \
                        and not a.get("isInline") and (a.get("size") or 0) >= 60_000
                    if not yra_pdf and not yra_nuotrauka and not vardas_a.lower().endswith(PRIEDU_PLETINIAI):
                        continue
                    tr = kl.get(f"{bazinis}/messages/{m['id']}/attachments/{a['id']}/$value")
                    if tr.status_code != 200:
                        nepavyko_priedu = True
                        continue
                    if yra_pdf or yra_nuotrauka:
                        priedai.append((nuo, vardas_a or ("saskaita.pdf" if yra_pdf else "nuotrauka.jpg"), tr.content, laisko_nr))
                    else:
                        laisku_info[laisko_nr]["kiti"].append(
                            (vardas_a, tr.content, _i_onedrive(kl, vardas_a, tr.content)))
                # "Matytas" TIK kai visi priedai parsisiusti (vienkartinis tinklo
                # trukis nebepaslepia laisko amzinai)
                if not nepavyko_priedu:
                    nauji_matyti.append(m["id"])

    rezultatas: dict[str, list[str]] = {}
    issaugoti_failai: dict[int, list[tuple[str, str]]] = {}  # laisko_nr -> [(fold, vardas)]
    ne_pdf = 0
    jau_buvo = 0
    buvo_istrinta = 0
    for siuntejas, vardas, baitai, laisko_nr in priedai:
        # %PDF ne visada pirmas baitas — kai kurie pasto serveriai prideda pries ji siuksliu.
        # NUOTRAUKOS (jpg/png) irgi priimamos — ju tipa nuspres klasifikacija (1 sluoksnis, vaizdas)
        yra_pdf = bool(baitai) and b"%PDF" in baitai[:1024]
        yra_vaizdas = bool(baitai) and (baitai[:3] == b"\xff\xd8\xff" or baitai[:8] == b"\x89PNG\r\n\x1a\n")
        if not yra_pdf and not yra_vaizdas:
            ne_pdf += 1
            continue
        if yra_vaizdas and not vardas.lower().endswith((".jpg", ".jpeg", ".png")):
            vardas = vardas + (".png" if baitai[:4] == b"\x89PNG" else ".jpg")
        hh = hashlib.sha256(baitai).hexdigest()
        if hh in esami:
            # PDF jau yra — bet laisko tekstas/priedai gali buti NAUJI:
            # prisegam prie ESAMO failo (papildyta informacija)
            if esami[hh]:
                issaugoti_failai.setdefault(laisko_nr, []).append(esami[hh])
            jau_buvo += 1
            continue

        # 1) zyme temoje „[Folderis]" (Power Automate kelias), 2) siuntejo taisykles
        fold = folderis_pagal_tema((laisku_info.get(laisko_nr) or {}).get("tema", ""), folderiai) \
            or folderis_pagal_siunteja(siuntejas, folderiai, priskyrimai)
        os.makedirs(os.path.join(darb_dir, fold), exist_ok=True)

        vardas = _saugus_vardas(vardas)
        kelias = os.path.join(darb_dir, fold, vardas)
        if os.path.exists(kelias):  # kitas turinys tuo paciu vardu
            vardas = f"{hh[:6]}_{vardas}"
            kelias = os.path.join(darb_dir, fold, vardas)
        with open(kelias, "wb") as f:
            f.write(baitai)
        esami[hh] = (fold, vardas)
        rezultatas.setdefault(fold, []).append(vardas)
        issaugoti_failai.setdefault(laisko_nr, []).append((fold, vardas))

    komentaru_atnaujinta = _irasyti_komentarus(failas, laisku_info, issaugoti_failai)

    if nauji_matyti:
        dabar = int(time.time())
        for mid in nauji_matyti:
            matyti_laiskai[mid] = dabar
        # sarasas nesikaupia amzinai — laikomi tik naujausi
        if len(matyti_laiskai) > 800:
            matyti_laiskai = dict(sorted(matyti_laiskai.items(), key=lambda x: -x[1])[:800])
        cfg = _cfg(failas)
        cfg["matyti_laiskai"] = matyti_laiskai
        _irasyti(failas, cfg)

    return {
        "folderiai": rezultatas,
        "komentaru_atnaujinta": komentaru_atnaujinta,
        "praleista": ne_pdf + jau_buvo + buvo_istrinta,
        # diagnostikai: kur nutruksta grandine (laiskai -> priedai -> issaugoti)
        "laiskai": laiskai,
        "priedai": len(priedai),
        "ne_pdf": ne_pdf,
        "jau_buvo": jau_buvo,
        "buvo_istrinta": buvo_istrinta,
    }


def busena(failas: str) -> dict:
    cfg = _cfg(failas)
    acc = cfg.get("paskyra")
    return {
        "paskyra": {"tiekejas": acc.get("tiekejas"), "email": acc.get("email")} if acc else None,
        "priskyrimai": cfg.get("priskyrimai") or {},
        "dezute": cfg.get("dezute") or "",
        "google_paruostas": paruostas(failas, "google"),
        "microsoft_paruostas": paruostas(failas, "microsoft"),
    }


def atjungti(failas: str) -> bool:
    cfg = _cfg(failas)
    if "paskyra" in cfg:
        del cfg["paskyra"]
        _irasyti(failas, cfg)
        return True
    return False

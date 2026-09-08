"""
AI ekstrakcija — PDF arba skenuota saskaita (jpg/png) -> struktūrizuoti duomenys.

Metodas perimtas is InvoPull SaaS (reference): pigus Haiku + „pasvarstyk pries JSON"
(chain-of-thought) + irankio schema + KODO validacija po to (sumu sutikrinimas).
Claude PDF palaikymas pats apdoroja ir tekstinius, ir skenuotus PDF.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

from anthropic import Anthropic

MODELIS = os.environ.get("EKSTRAKCIJOS_MODELIS", "claude-haiku-4-5")

# Kiek daugiausiai tokenu AI gali israsyti atsakydama. Didele saskaita (keli
# puslapiai, daug eiluciu) i 8000 NETELPA — atsakymas nutrukdavo viduryje ir
# grazindavo tuscia saskaita, nors tokenai jau buvo apmoketi. Sonnet 5 leidzia
# iki 128 000; imam su atsarga ir, jei vis tiek nutruksta, kartojam dvigubai.
MAX_TOKENAI = max(4000, int(os.environ.get("EKSTRAKCIJOS_MAX_TOKENAI") or 24000))
TOKENU_RIBA = 64000

# Kainos $/1M tokenu PAGAL MODELI (in, out) — islaidu apskaitai admin paneleje.
# claude-sonnet-5 oficialiai $3/$15 (iki 2026-08-31 galioja ivadine $2/$10 — skaiciuojam pilna).
KAINOS = {
    "flash-lite": (0.10, 0.40),
    "flash": (0.30, 2.50),  # Gemini Flash tier (apytiksle — 2.5 kainorastis)
    "pro": (2.00, 12.00),   # Gemini Pro tier (apytiksle; klasifikacijos sietui — 1 psl.)
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (5.00, 25.00),
}


def _modelio_kainos(modelis: str) -> tuple[float, float]:
    for zodis, kainos in KAINOS.items():
        if zodis in (modelis or ""):
            return kainos
    return KAINOS["sonnet"]


def kaina_ct(modelis: str, tok_in: int, tok_out: int) -> float:
    kin, kout = _modelio_kainos(modelis)
    return round((tok_in * kin + tok_out * kout) / 1_000_000 * 100, 2)

IRANKIS = {
    "name": "pateikti_saskaita",
    "description": "Pateikti pilnai istrauktus pirkimo saskaitos duomenis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "yra_pirkimo_saskaita": {"type": "boolean", "description": "TRUE tik jei dokumentas yra tikra PVM saskaita-faktura arba kreditine saskaita uz JAU suteiktas prekes/paslaugas. FALSE: isankstine/avansine/proforma saskaita (apmokejimas pries pristatyma), pasiulymas, uzsakymas, sutartis, vaztarastis, kitas dokumentas."},
            "dokumento_tipas": {"type": "string", "description": "Vienas is: pvm_saskaita, kreditine, isankstine, pasiulymas, uzsakymas, sutartis, vaztarastis, kita."},
            "tiekejas_pavadinimas": {"type": "string"},
            "tiekejas_imones_kodas": {"type": "string", "description": "Imones kodas (tik skaitmenys)."},
            "tiekejas_pvm_kodas": {"type": "string", "description": "PVM moketojo kodas, pvz. LT100001234567."},
            "tiekejas_gatve": {"type": "string", "description": "Tiekejo gatve ir namo nr, pvz. Ateities g. 15."},
            "tiekejas_miestas": {"type": "string", "description": "Tiekejo miestas."},
            "tiekejas_pasto_kodas": {"type": "string", "description": "Tiekejo pasto kodas, jei nurodytas."},
            "pirkejas_pavadinimas": {"type": "string"},
            "pirkejas_imones_kodas": {"type": "string"},
            "pirkejas_pvm_kodas": {"type": "string"},
            "pirkejas_miestas": {"type": "string", "description": "Pirkejo miestas."},
            "saskaitos_numeris": {"type": "string", "description": "PILNAS numeris su serija, pvz. SF-2026-010. Serijos raides TIKSLIAI kaip dokumente, iskaitant lietuviskas."},
            "saskaitos_data": {"type": "string", "description": "YYYY-MM-DD"},
            "apmoketi_iki": {"type": "string", "description": "YYYY-MM-DD arba tuscia."},
            "valiuta": {"type": "string", "description": "pvz. EUR"},
            "eilutes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pavadinimas": {"type": "string", "description": "Prekes pavadinimas TIKSLIAI kaip dokumente."},
                        "tiekejo_kodas": {"type": "string", "description": "Tiekejo prekes kodas, jei matomas (kitaip tuscia)."},
                        "ean": {"type": "string", "description": "EAN/barkodas, jei matomas (kitaip tuscia)."},
                        "kiekis": {"type": "number"},
                        "vienetas": {"type": "string", "description": "vnt, kompl, m, m2, kg, l, pak..."},
                        "vnt_kaina": {"type": "number", "description": "GALUTINE vieneto kaina BE PVM (po nuolaidos, jei ji taikoma)."},
                        "suma": {"type": "number", "description": "Eilutes suma BE PVM."},
                        "pvm_proc": {"type": "number", "description": "PVM tarifas procentais (21, 9, 5, 0)."},
                    },
                    "required": ["pavadinimas", "kiekis", "vienetas", "vnt_kaina", "suma", "pvm_proc"],
                },
            },
            "suma_be_pvm": {"type": "number", "description": "Dokumente NURODYTA suma be PVM."},
            "pvm_suma": {"type": "number", "description": "Dokumente NURODYTA PVM suma."},
            "suma_su_pvm": {"type": "number", "description": "Dokumente NURODYTA suma su PVM (VISO)."},
        },
        "required": ["yra_pirkimo_saskaita", "tiekejas_pavadinimas", "saskaitos_numeris",
                     "saskaitos_data", "eilutes", "suma_be_pvm", "pvm_suma", "suma_su_pvm"],
    },
}

PROMPTAS = """Istrauk VISUS duomenis is sios pirkimo saskaitos.

PIRMIAUSIA PASVARSTYK TEKSTE (pries kviesdamas iranki):
1. Israsyk KIEKVIENA prekes eilute: pavadinimas, kiekis x vieneto kaina = suma.
2. Susumuok visas eilutes ir palygink su dokumente nurodyta suma be PVM.
3. Jei nesutampa — PERSVARSTYK stulpelius. Dazna klaida: jei eiluteje matoma tik VIENA
   pinigu reiksme, ji gali buti EILUTES SUMA (tada vieneto kaina = suma / kiekis), o ne vieneto kaina.
4. NUOLAIDOS: jei eiluteje ar dokumente yra nuolaida (procentas ar suma), vnt_kaina ir suma
   imamos GALUTINES — po nuolaidos. Pozymis, kad paemei ne ta stulpeli: kiekis x tavo kaina
   nesutampa su eilutes galutine suma.
5. Patikrink: suma_be_pvm + pvm_suma = suma_su_pvm.
TIK KAI matematika sueina — iskviesk pateikti_saskaita.

Taisykles:
- DOKUMENTO TIPAS. Pirkimo saskaita (yra_pirkimo_saskaita=true) yra TIK PVM saskaita
  faktura arba kreditine saskaita — dokumentas, pagal kuri pirkejas JAU skolingas uz
  gautas prekes ar paslaugas. NE pirkimo saskaita (false): isankstine / avansine /
  proforma saskaita (apmokejimas PRIES pristatyma — net jei pavadinta "isankstine PVM
  saskaita faktura"), pasiulymas ar kainu pasiulymas, uzsakymas, sutartis, vaztarastis.
  Tokiems dokumentams vis tiek uzpildyk kitus laukus kiek matai, bet yra_pirkimo_saskaita=false
  ir nurodyk dokumento_tipa.
- Skaiciai: kablelis = desimtainis skirtukas (1.234,56 -> 1234.56).
- Eilutes IMK VISAS, kiek ju yra. Nuolaidu/transporto eilutes irgi yra eilutes.
- Prekes pavadinimas dokumente daznai LUZTA i kelias teksto eilutes (tarp daliu
  gali isiterpti kiekiai ar kainos). Surink VISAS pavadinimo dalis ta pacia
  tvarka ir nepraleisk NE VIENO zodzio, gamintojo vardo, kodo ar skaiciaus.
- Sumos (suma_be_pvm, pvm_suma, suma_su_pvm) — TIK tos, kurios NURODYTOS dokumente, ne tavo apskaiciuotos.
- PDF teksto sluoksnyje lietuviskos raides kartais buna sugadintos (vietoj raides —
  klaustukas ar keistas zenklas). Tokia raide atstatyk pagal MATOMA dokumento vaizda.
  Ypac svarbu saskaitos serijoje/numeryje: raidziu nepraleisk ir nepakeisk kitomis.
- Ko nera dokumente — palik tuscia, NIEKO neisgalvok."""


def _teksto_sluoksnis_brokuotas(baitai: bytes) -> bool:
    """PDF su sriftu be simboliu zemelapio: akims raides atrodo gerai, bet
    teksto sluoksnyje jos virsta ? — ir modeli pasiekia jau BE tu raidziu."""
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(baitai))
        tekstas = "".join((p.extract_text() or "") for p in r.pages[:5])
        return "�" in tekstas
    except Exception:
        return False


def _puslapiu_nuotraukos(baitai: bytes) -> list[bytes]:
    """Puslapiai kaip JPEG (~170 DPI) — modelis skaito akimis, be teksto sluoksnio."""
    import pymupdf
    dok = pymupdf.open(stream=baitai, filetype="pdf")
    try:
        return [dok[i].get_pixmap(matrix=pymupdf.Matrix(2.4, 2.4)).tobytes("jpeg", jpg_quality=85)
                for i in range(min(dok.page_count, 10))]
    finally:
        dok.close()


def _siuntimo_gabalai(baitai: bytes, mime: str) -> list[tuple[str, bytes]]:
    """Ka siusti modeliui. Paprastai — pati faila (pigiausia). Bet jei PDF
    teksto sluoksnis brokuotas, tekstu pasikliauti negalima — siunciam puslapiu
    nuotraukas, kad modelis viska skaitytu is vaizdo (brangiau, todel tik cia)."""
    if mime == "application/pdf" and _teksto_sluoksnis_brokuotas(baitai):
        try:
            nuotraukos = _puslapiu_nuotraukos(baitai)
            if nuotraukos:
                print(f"[ekstrakcija] brokuotas PDF teksto sluoksnis -> siunciam {len(nuotraukos)} psl. nuotraukomis", flush=True)
                return [("image/jpeg", n) for n in nuotraukos]
        except Exception as e:
            print(f"[ekstrakcija] nuotrauku parengti nepavyko ({e}) — siunciamas PDF kaip iprasta", flush=True)
    return [(mime, baitai)]


def _blokas(baitai: bytes, mime: str):
    b64 = base64.standard_b64encode(baitai).decode()
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}


def _kvietimas(client, zinutes: list, limitas: int, priverstinai: bool):
    """Vienas kvietimas SRAUTU. Srautas butinas, nes prie didelio limito
    paprastas kvietimas nutruksta pagal HTTP laukimo laika, o ne pagal atsakyma."""
    with client.messages.stream(
        model=MODELIS,
        max_tokens=limitas,
        messages=zinutes,
        tools=[IRANKIS],
        tool_choice={"type": "tool", "name": "pateikti_saskaita"} if priverstinai else {"type": "auto"},
    ) as srautas:
        return srautas.get_final_message()


# --- Gemini kelias (EKSTRAKCIJOS_MODELIS=gemini-...) -------------------------
# Gemini negrazina teksto pries JSON (atsakymas TIK JSON pagal schema), bet
# Flash modeliai svarsto viduje (thinking) — tas pats „pasvarstyk pries JSON".

PROMPTAS_GEMINI = (
    PROMPTAS
    .replace("PIRMIAUSIA PASVARSTYK TEKSTE (pries kviesdamas iranki):", "PIRMIAUSIA PASVARSTYK (mintyse, pries rasydamas JSON):")
    .replace("TIK KAI matematika sueina — iskviesk pateikti_saskaita.", "TIK KAI matematika sueina — surasyk galutini JSON.")
)


def _gemini_schema(s: dict) -> dict:
    """Musu irankio JSON schema -> Gemini responseSchema (tipai DIDZIOSIOMIS)."""
    out = {}
    for k, v in s.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties":
            out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


def _gemini_post(modelis: str, dalys: list, schema: dict, limitas: int) -> dict:
    """Vienas Gemini kvietimas su JSON schema atsakymui; kartoja esant perkrovai."""
    raktas = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelis}:generateContent?key={raktas}"
    kunas = json.dumps({
        "contents": [{"role": "user", "parts": dalys}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
            "maxOutputTokens": limitas,
            "temperature": 0,
        },
    }).encode()
    klaida = None
    for bandymas in range(4):
        try:
            uzkl = urllib.request.Request(url, data=kunas, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(uzkl, timeout=900) as ats:
                return json.loads(ats.read().decode())
        except urllib.error.HTTPError as e:
            klaida = f"Gemini HTTP {e.code}: {e.read().decode()[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # tinklo trukis ir pan.
            klaida = f"Gemini: {e}"
        time.sleep(2 ** bandymas)
    raise RuntimeError(klaida or "Gemini nepasieke")


def _gemini_tekstas(ats: dict) -> str:
    kand = (ats.get("candidates") or [{}])[0]
    return "".join(p.get("text") or "" for p in ((kand.get("content") or {}).get("parts") or []))


def _gemini_naudota(ats: dict) -> dict:
    um = ats.get("usageMetadata") or {}
    return {"tokenai_in": um.get("promptTokenCount") or 0,
            "tokenai_out": (um.get("candidatesTokenCount") or 0) + (um.get("thoughtsTokenCount") or 0)}


def _gemini_kvietimas(baitai: bytes, mime: str, limitas: int) -> dict:
    dalys = [{"inline_data": {"mime_type": m, "data": base64.standard_b64encode(b).decode()}}
             for m, b in _siuntimo_gabalai(baitai, mime)]
    dalys.append({"text": PROMPTAS_GEMINI})
    return _gemini_post(MODELIS, dalys, IRANKIS["input_schema"], limitas)


def _gemini_istraukti(baitai: bytes, mime: str) -> dict:
    tok_in = tok_out = 0
    limitas = MAX_TOKENAI
    for bandymas in (1, 2):
        ats = _gemini_kvietimas(baitai, mime, min(limitas, 65536))
        um = ats.get("usageMetadata") or {}
        tok_in += um.get("promptTokenCount") or 0
        tok_out += (um.get("candidatesTokenCount") or 0) + (um.get("thoughtsTokenCount") or 0)

        kand = (ats.get("candidates") or [{}])[0]
        priezastis = kand.get("finishReason")
        if priezastis == "MAX_TOKENS":
            if bandymas == 1 and limitas < TOKENU_RIBA:
                limitas = min(limitas * 2, TOKENU_RIBA)
                continue
            raise RuntimeError(f"Saskaita per didele: AI atsakymas nesutilpo i {limitas} tokenu")
        if priezastis not in (None, "STOP"):
            raise RuntimeError(f"Gemini nutrauke atsakyma: {priezastis}")

        dalys = (kand.get("content") or {}).get("parts") or []
        tekstas = "".join(p.get("text") or "" for p in dalys)
        if not tekstas.strip():
            raise RuntimeError("Gemini grazino tuscia atsakyma")
        try:
            duomenys = json.loads(tekstas)
        except ValueError:
            raise RuntimeError("Gemini grazino netinkama JSON")

        duomenys["_ai"] = {
            "modelis": ats.get("modelVersion") or MODELIS,
            "tokenai_in": tok_in,
            "tokenai_out": tok_out,
            "kaina_ct": kaina_ct(MODELIS, tok_in, tok_out),
        }
        return duomenys
    raise RuntimeError("Gemini nepateike rezultato")


def _numerio_patikslinimas(numeris: str, baitai: bytes, mime: str) -> str:
    """AI kartais pameta raide numerio pradzioje (Google apdorojimas kai kuriu
    sriftu lietuviskas raides isbarsto). PDF teksto sluoksnis — patikimiausias
    saltinis: jei jame pries TA PACIA skaitmenu seka stovi ILGESNIS raidziu
    zodis, kuris baigiasi AI grazintomis raidemis — imam pilna. 0 tokenu."""
    if mime != "application/pdf" or not numeris:
        return numeris
    # AI numeri grazina ivairiai ("RA Nr. 0139566", "RA-0139566") — pries
    # lyginant nuimam "Serija"/"Nr." zodzius ir tarpus, kaip visur sistemoje
    n = re.sub(r"(?i)^\s*serija\b", "", str(numeris))
    n = re.sub(r"(?i)\bnr\.?(?=[\s\d]|$)", "", n)
    n = re.sub(r"[\s\-]+", "", n)
    m = re.match(r"^([^\W\d_]{1,6})(\d{5,})$", n)
    if not m:
        return numeris
    ai_raides, skaitmenys = m.group(1).upper(), m.group(2)
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(baitai))
        tekstas = "".join((p.extract_text() or "") for p in r.pages[:3])
    except Exception:
        return numeris
    sablonas = r"([^\W\d_]{1,6})\s*(?:[Nn]r\.?\s*)?[\s\-]*" + re.escape(skaitmenys)
    for mt in re.finditer(sablonas, tekstas):
        zodis = mt.group(1)
        if zodis.upper() != ai_raides and zodis.upper().endswith(ai_raides) and len(zodis) > len(ai_raides):
            return zodis + skaitmenys
    return numeris


def istraukti(baitai: bytes, mime: str) -> dict:
    duomenys = _istraukti_modeliu(baitai, mime)
    try:
        nr = duomenys.get("saskaitos_numeris") or ""
        naujas = _numerio_patikslinimas(nr, baitai, mime)
        if naujas != nr:
            print(f"[ekstrakcija] numeris patikslintas is teksto sluoksnio: {nr} -> {naujas}", flush=True)
            duomenys["saskaitos_numeris"] = naujas
    except Exception as e:
        print(f"[ekstrakcija] numerio patikslinti nepavyko: {e}", flush=True)
    return duomenys


def _istraukti_modeliu(baitai: bytes, mime: str) -> dict:
    if MODELIS.startswith("gemini"):
        return _gemini_istraukti(baitai, mime)
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    turinys = [_blokas(b, m) for m, b in _siuntimo_gabalai(baitai, mime)]
    turinys.append({"type": "text", "text": PROMPTAS})
    zinutes = [{"role": "user", "content": turinys}]

    tok_in = tok_out = 0
    limitas = MAX_TOKENAI
    irankio = None

    for bandymas in (1, 2):
        msg = _kvietimas(client, zinutes, limitas, False)
        tok_in += msg.usage.input_tokens
        tok_out += msg.usage.output_tokens

        if msg.stop_reason == "max_tokens":
            # Atsakymas nesutilpo — dalinis irankio kvietimas butu su tusciais
            # laukais, todel jo NEIMAM. Kartojam su dvigubu limitu.
            if bandymas == 1 and limitas < TOKENU_RIBA:
                limitas = min(limitas * 2, TOKENU_RIBA)
                continue
            raise RuntimeError(
                f"Saskaita per didele: AI atsakymas nesutilpo i {limitas} tokenu")

        irankio = next((b for b in msg.content if b.type == "tool_use"), None)
        if irankio is not None:
            break

        # Modelis pasvarste, bet nepakviete irankio — antras zingsnis: priverstinai.
        tekstas = "".join(b.text for b in msg.content if b.type == "text")
        msg2 = _kvietimas(client, zinutes + [
            {"role": "assistant", "content": tekstas or "Pasvarstyta."},
            {"role": "user", "content": "Dabar pateik rezultata per pateikti_saskaita iranki."},
        ], limitas, True)
        tok_in += msg2.usage.input_tokens
        tok_out += msg2.usage.output_tokens

        if msg2.stop_reason == "max_tokens":
            if bandymas == 1 and limitas < TOKENU_RIBA:
                limitas = min(limitas * 2, TOKENU_RIBA)
                continue
            raise RuntimeError(
                f"Saskaita per didele: AI atsakymas nesutilpo i {limitas} tokenu")

        irankio = next((b for b in msg2.content if b.type == "tool_use"), None)
        if irankio is None:
            raise RuntimeError("AI nepateike strukturuoto rezultato")
        break

    if irankio is None:
        raise RuntimeError("AI nepateike strukturuoto rezultato")

    duomenys = dict(irankio.input)
    duomenys["_ai"] = {
        "modelis": MODELIS,
        "tokenai_in": tok_in,
        "tokenai_out": tok_out,
        "kaina_ct": kaina_ct(MODELIS, tok_in, tok_out),
    }
    return duomenys


# Atsakymai grizta SU EILUTES NUMERIU, o kodas juos sudeda pagal numeri.
# Todel pasislinkimas (AI ilgame sarase praleidzia eilute -> visi tolesni
# atsakymai suplaukia per viena) nebegalimas: praleista eilute tiesiog lieka
# tuscia, o kitos atsigula i savo vietas.
SINONIMU_IRANKIS = {
    "name": "pateikti_sinonimus",
    "description": "Pateikti prekiu pavadinimu sinonimus pagal eiluciu numerius.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sinonimai": {
                "type": "array",
                "description": "Irasai TIK toms eilutems, kurioms radai sinonima; kitu eiluciu visai neminek.",
                "items": {
                    "type": "object",
                    "properties": {
                        "eilute": {"type": "integer", "description": "Eilutes numeris is uzklausos (1, 2, 3...)."},
                        "sinonimas": {"type": "string", "description": "Perrasytas pavadinimas su katalogo terminu (visi parametrai, dydziai, spalvos palikti)."},
                    },
                    "required": ["eilute", "sinonimas"],
                },
            },
        },
        "required": ["sinonimai"],
    },
}


def sinonimai_pagal_terminus(pavadinimai: list[str], terminu_sarasai: list[list[str]]) -> tuple[list[str], dict]:
    """Kiekvienai eilutei AI RENKASI is MUSU katalogo terminu (ne kuria pats — be haliucinaciju):
    jei katalogas ta pati prekes tipa vadina kitu zodziu, eilute perrasoma tuo terminu.
    Terminai paimami is vektoriskai artimiausiu katalogo korteliu — veikia bet kokiam katalogui."""
    tuscia = {"tokenai_in": 0, "tokenai_out": 0}
    if not pavadinimai:
        return [], tuscia
    # Kai ekstrakcija ant Gemini — ir sinonimai per ta pati rakta (Flash-lite,
    # pigiausias pakopos modelis); kitaip — tas pats Anthropic modelis.
    modelis = os.environ.get("SINONIMU_MODELIS") or (
        "gemini-3.5-flash-lite" if MODELIS.startswith("gemini") else MODELIS)
    eil = []
    for i, (p, terms) in enumerate(zip(pavadinimai, terminu_sarasai), 1):
        eil.append(f"{i}. {p}\n   Katalogo terminai: {', '.join(terms) if terms else '(nera)'}")
    zinute = f"""Tiekejo saskaitos eilutes ir MUSU katalogo terminai (preku tipai, kaip jie vadinami kataloge).
Kiekvienai eilutei: jei tarp terminu yra zodis, ivardijantis TA PATI GAMINI kitu pavadinimu (sinonimas) —
perrasyk VISA eilutes pavadinima, pakeisdamas tik tipo zodi tuo terminu (visus parametrus, dydzius,
spalva PALIK eiluteje). Prie kiekvieno atsakymo nurodyk EILUTES NUMERI is saraso.

GRIEZTA taisykle: perrasyk TIK kai tai tikrai TAS PATS daiktas kitu vardu. Jei terminas ivardija
KITA gamini — net panasu, giminingo tipo ar tos pacios srities — eilutes visai neminek.
Nemink eilutes ir kai jos tipo zodis jau sutampa su katalogo terminu.
NEKURK savo terminu — naudok tik duotus.

{chr(10).join(eil)}"""

    if modelis.startswith("gemini"):
        ats = _gemini_post(modelis, [{"text": zinute}], SINONIMU_IRANKIS["input_schema"], 4000)
        naudota = _gemini_naudota(ats)
        try:
            irasai = (json.loads(_gemini_tekstas(ats)) or {}).get("sinonimai") or []
        except ValueError:
            irasai = []
    else:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=modelis,
            max_tokens=1500,
            messages=[{"role": "user", "content": zinute}],
            tools=[SINONIMU_IRANKIS],
            tool_choice={"type": "tool", "name": "pateikti_sinonimus"},
        )
        irankio = next((b for b in msg.content if b.type == "tool_use"), None)
        irasai = (irankio.input.get("sinonimai") if irankio else None) or []
        naudota = {"tokenai_in": msg.usage.input_tokens, "tokenai_out": msg.usage.output_tokens}

    # Surenkam PAGAL NUMERI — ne pagal vieta sarase
    out = [""] * len(pavadinimai)
    for irasas in irasai:
        try:
            idx = int(irasas.get("eilute")) - 1
            s = str(irasas.get("sinonimas") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= idx < len(out) and s:
            out[idx] = s
    # apsauga: identiskas uzrasas = ne sinonimas
    out = ["" if s.lower() == (pavadinimai[i] or "").strip().lower() else s for i, s in enumerate(out)]
    return out, naudota


def patikrinti(d: dict) -> list[str]:
    """KODO validacija (0 tokenu): sumu sutikrinimas kaip SaaS validate-extraction."""
    isp = []
    eilutes = d.get("eilutes") or []
    if not eilutes:
        isp.append("Neistraukta ne viena eilute.")
        return isp

    tol = 0.05 + 0.01 * len(eilutes)
    eil_suma = round(sum(float(e.get("suma") or 0) for e in eilutes), 2)
    be_pvm = float(d.get("suma_be_pvm") or 0)
    pvm = float(d.get("pvm_suma") or 0)
    viso = float(d.get("suma_su_pvm") or 0)

    if be_pvm and abs(eil_suma - be_pvm) > tol:
        isp.append(f"Eiluciu suma ({eil_suma:.2f}) nesutampa su nurodyta suma be PVM ({be_pvm:.2f}).")
    if viso and be_pvm and abs((be_pvm + pvm) - viso) > 0.05:
        isp.append(f"be PVM ({be_pvm:.2f}) + PVM ({pvm:.2f}) nesutampa su VISO ({viso:.2f}).")

    for i, e in enumerate(eilutes, 1):
        k = float(e.get("kiekis") or 0)
        vk = float(e.get("vnt_kaina") or 0)
        s = float(e.get("suma") or 0)
        if k and vk and s and abs(round(k * vk, 2) - s) > 0.02:
            isp.append(f"{i} eil.: kiekis x kaina ({round(k*vk,2):.2f}) nesutampa su suma ({s:.2f}).")
    return isp

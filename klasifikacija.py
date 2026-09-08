"""
DOKUMENTO TIPO SIETAS pries pilna AI skaityma — trys sluoksniai, nuo pigiausio.
Tikslas: i pajamavima nepatektu isankstines (proforma/avansines) saskaitos,
pasiulymai, uzsakymai, sutartys, nuotraukos — ir kad tikrinimas kainuotu
beveik nieko (Power Automate is darbuotoju pastu atnesa visko).

  0 sluoksnis — 0 tokenu. PDF teksto sluoksnis: nera saskaitos pozymiu -> ne
    saskaita. Yra pozymiu, trumpas dokumentas, antrasteje nera neigiamu zodziu
    (isankstine, proforma, pasiulymas...) -> tikra saskaita, tiesiai i skaityma.
  1 sluoksnis — MAZAS klausimas STIPRESNIAM modeliui tik is PIRMO puslapio
    (tekstas arba vaizdas): ~2 000 tokenu, todel net brangus modelis kainuoja
    puse cento. Cia patenka: neigiamu zodziu turintys, ilgi (>=6 psl.),
    be teksto sluoksnio ir nuotraukos (jpg/png).
  2 sluoksnis — pilna ekstrakcija (ekstrakcija.py), kuri irgi grazina
    dokumento_tipa — antra nuomone. Nesutarus — zmogui geltona zyme, ne tyla.

Modelis 1 sluoksniui: .env KLASIFIKACIJOS_MODELIS (numatyta gemini-3.1-pro-preview —
naujausias Google „pro" 2026-09; gemini-2.5-pro — stabilus atsarginis);
jei nepasiekiamas — krentama i EKSTRAKCIJOS_MODELIS.
"""

import base64
import io
import json
import os
import unicodedata

import ekstrakcija

MODELIS = os.environ.get("KLASIFIKACIJOS_MODELIS", "gemini-3.1-pro-preview")

# Saskaitos pozymiai (be diakritikos, mazosiomis) — 0 tokenu
POZYMIAI = ("saskaita faktura", "saskaita-faktura", "faktura", "invoice", "rechnung", "factuur")
# NEIGIAMI pozymiai antrastes zonoje (pirmi ~1500 simboliu): dokumentas gali
# vadintis „isankstine PVM saskaita faktura" — turi pozymi, bet NE pirkimas
NEIGIAMI = ("isankstin", "proforma", "pro forma", "avansin", "pasiulym", "komercinis",
            "samata", "uzsakym", "sutartis", "quotation", "quote", "offer", "order confirmation",
            "proforma invoice", "advance invoice")

TIPAI = ("pvm_saskaita", "kreditine", "isankstine", "pasiulymas", "uzsakymas", "sutartis",
         "vaztarastis", "nuotrauka", "kita")

SCHEMA = {
    "type": "object",
    "properties": {
        "dokumento_tipas": {"type": "string", "description": "Vienas is: " + ", ".join(TIPAI)},
        "yra_pirkimo_saskaita": {"type": "boolean", "description": "TRUE tik jei tikra PVM saskaita faktura arba kreditine uz JAU suteiktas prekes/paslaugas."},
        "pardavejo_kodas": {"type": "string", "description": "Pardavejo imones kodas, jei matomas (tik skaitmenys), kitaip tuscia."},
        "pagrindas": {"type": "string", "description": "Trumpai (iki 15 zodziu), kodel toks tipas."},
    },
    "required": ["dokumento_tipas", "yra_pirkimo_saskaita"],
}

PROMPTAS = (
    "Pasakyk, KOKS tai dokumentas — matai tik jo PIRMA puslapi.\n"
    "Pirkimo saskaita (yra_pirkimo_saskaita=true) yra TIK PVM saskaita faktura arba "
    "kreditine saskaita uz JAU suteiktas prekes ar paslaugas.\n"
    "NE pirkimo saskaita: isankstine / avansine / proforma saskaita (apmokejimas PRIES "
    "pristatyma — net jei pavadinta 'isankstine PVM saskaita faktura'), pasiulymas ar "
    "kainu pasiulymas, uzsakymas, sutartis, vaztarastis, nuotrauka be dokumento, kita.\n"
    "Atsakyk tik JSON pagal schema."
)


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _pdf_tekstas(baitai: bytes, psl: int = 4) -> tuple[str, int]:
    """(pirmu puslapiu tekstas, puslapiu skaicius). Tuscia — nera teksto sluoksnio."""
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(baitai))
        return "".join((p.extract_text() or "") for p in r.pages[:psl]), len(r.pages)
    except Exception:
        return "", 0


def _pirmo_puslapio_vaizdas(baitai: bytes) -> bytes | None:
    try:
        import pymupdf
        dok = pymupdf.open(stream=baitai, filetype="pdf")
        try:
            return dok[0].get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0)).tobytes("jpeg", jpg_quality=80)
        finally:
            dok.close()
    except Exception:
        return None


def _ai_klausimas(dalys_gemini: list, turinys_anthropic: list, modelis: str) -> dict:
    if modelis.startswith("gemini"):
        ats = ekstrakcija._gemini_post(modelis, dalys_gemini + [{"text": PROMPTAS}], SCHEMA, 1500)
        tekstas = ekstrakcija._gemini_tekstas(ats)
        d = json.loads(tekstas) if tekstas.strip() else {}
        n = ekstrakcija._gemini_naudota(ats)
        d["_ai"] = {"modelis": ats.get("modelVersion") or modelis, **n,
                    "kaina_ct": ekstrakcija.kaina_ct(modelis, n["tokenai_in"], n["tokenai_out"])}
        return d
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=modelis, max_tokens=600,
        messages=[{"role": "user", "content": turinys_anthropic + [{"type": "text", "text": PROMPTAS}]}],
        tools=[{"name": "pateikti_tipa", "description": "Dokumento tipas", "input_schema": SCHEMA}],
        tool_choice={"type": "tool", "name": "pateikti_tipa"},
    )
    d = next((b.input for b in msg.content if b.type == "tool_use"), {})
    d["_ai"] = {"modelis": modelis, "tokenai_in": msg.usage.input_tokens, "tokenai_out": msg.usage.output_tokens,
                "kaina_ct": ekstrakcija.kaina_ct(modelis, msg.usage.input_tokens, msg.usage.output_tokens)}
    return d


def _ai_tipas(tekstas: str, vaizdas: bytes | None, vaizdo_mime: str) -> dict:
    """1 sluoksnis: mazas klausimas — tik pirmo puslapio tekstas ARBA vaizdas.
    Stipresnis modelis; jei nepasiekiamas — ekstrakcijos modelis."""
    if tekstas.strip():
        dalys_g = [{"text": "Dokumento pirmo puslapio tekstas:\n\n" + tekstas[:6000]}]
        turinys_a = [{"type": "text", "text": "Dokumento pirmo puslapio tekstas:\n\n" + tekstas[:6000]}]
    elif vaizdas:
        b64 = base64.standard_b64encode(vaizdas).decode()
        dalys_g = [{"inline_data": {"mime_type": vaizdo_mime, "data": b64}}]
        turinys_a = [{"type": "image", "source": {"type": "base64", "media_type": vaizdo_mime, "data": b64}}]
    else:
        return {"dokumento_tipas": "kita", "yra_pirkimo_saskaita": True, "_ai": {"kaina_ct": 0}}
    try:
        return _ai_klausimas(dalys_g, turinys_a, MODELIS)
    except Exception as e:
        print(f"[klasifikacija] {MODELIS} nepasiekiamas ({str(e)[:120]}) — bandom {ekstrakcija.MODELIS}", flush=True)
        return _ai_klausimas(dalys_g, turinys_a, ekstrakcija.MODELIS)


def ivertinti(baitai: bytes, mime: str) -> dict:
    """Grazina {"saskaita": bool, "tipas": str, "sluoksnis": 0|1, "kaina_ct": float,
    "pagrindas": str}. Abejones atveju saskaita=True — sprendzia pilna ekstrakcija."""
    if mime != "application/pdf":
        # NUOTRAUKA / paveiksliukas: teksto nera — is karto 1 sluoksnis (vaizdas)
        d = _ai_tipas("", baitai, mime)
        return _rez(d, 1)
    tekstas, psl = _pdf_tekstas(baitai)
    t = _norm(tekstas)
    if len(t.strip()) >= 80:
        if not any(p in t for p in POZYMIAI):
            return {"saskaita": False, "tipas": "kita", "sluoksnis": 0, "kaina_ct": 0,
                    "pagrindas": "tekste nėra sąskaitos požymių"}
        antraste = t[:1500]
        neigiamas = any(n in antraste for n in NEIGIAMI)
        if not neigiamas and psl < 6:
            return {"saskaita": True, "tipas": "pvm_saskaita", "sluoksnis": 0, "kaina_ct": 0,
                    "pagrindas": "sąskaitos požymiai, trumpas dokumentas"}
        # neigiamu zodziu antrasteje ARBA ilgas dokumentas -> mazas klausimas AI
        d = _ai_tipas(tekstas, None, "")
        return _rez(d, 1)
    # be teksto sluoksnio (skenuota) — pirmo puslapio vaizdas modeliui
    d = _ai_tipas("", _pirmo_puslapio_vaizdas(baitai), "image/jpeg")
    return _rez(d, 1)


def _rez(d: dict, sluoksnis: int) -> dict:
    tipas = str(d.get("dokumento_tipas") or "kita")
    saskaita = bool(d.get("yra_pirkimo_saskaita", True)) and tipas not in (
        "isankstine", "pasiulymas", "uzsakymas", "sutartis", "vaztarastis", "nuotrauka")
    return {"saskaita": saskaita, "tipas": tipas, "sluoksnis": sluoksnis,
            "kaina_ct": (d.get("_ai") or {}).get("kaina_ct") or 0,
            "modelis": (d.get("_ai") or {}).get("modelis") or "",
            "pagrindas": str(d.get("pagrindas") or ""),
            "pardavejo_kodas": str(d.get("pardavejo_kodas") or "")}

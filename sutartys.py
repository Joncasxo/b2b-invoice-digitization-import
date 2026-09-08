"""
SUTARTINIAI IKAINIAI — tiekeju sutarciu/pasiulymu kainynai PARDAVIMO kainai.

Vadybininkas saskaitos eiluteje parenka sutarties pozicija -> pardavimo suma
suskaiciuojama kiekis x ikainis (be PVM). Pajamavimo (pirkimo) skaiciu neliecia.

Saugoma kiekvienos aplinkos faile sutartys.json:
  {"sutartys": [{"numeris", "tiekejas", "pozicijos": [{"pavadinimas",
                 "vienetas", "ikainis_be_pvm"}], "atnaujinta"}]}

Ikainiu ISTRAUKIMAS is sutarties PDF — ta pati AI infrastruktura kaip saskaitu
(ekstrakcija.py): Gemini arba Anthropic pagal EKSTRAKCIJOS_MODELIS.
"""

import json
import os
from datetime import datetime

import ekstrakcija

IRANKIS = {
    "name": "pateikti_ikainius",
    "description": "Pateik is sutarties/pasiulymo istrauktus sutartinius ikainius.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sutarties_numeris": {"type": "string", "description": "Sutarties numeris TIKSLIAI kaip dokumente"},
            "tiekejas": {"type": "string", "description": "PARDAVEJO (kuris teikia prekes/paslaugas siais ikainiais) pavadinimas — ne pirkejo"},
            "pozicijos": {"type": "array", "items": {"type": "object", "properties": {
                "pavadinimas": {"type": "string", "description": "Pozicijos pavadinimas tiksliai kaip dokumente"},
                "vienetas": {"type": "string", "description": "Matavimo vienetas (vnt, m3, t, val...)"},
                "ikainis_be_pvm": {"type": "number", "description": "Vieneto kaina BE PVM"},
            }, "required": ["pavadinimas", "ikainis_be_pvm"]}},
        },
        "required": ["pozicijos"],
    },
}

PROMPTAS = (
    "Dokumentas — tiekejo sutartis arba pasiulymas su sutartiniais ikainiais.\n"
    "Istrauk VISAS pozicijas, turincias vieneto kaina: pavadinimas TIKSLIAI kaip "
    "dokumente, matavimo vienetas ir vieneto kaina BE PVM.\n"
    "Jei kaina dokumente nurodyta su PVM — perskaiciuok i be PVM pagal dokumente "
    "nurodyta tarifa.\n"
    "Sutarties numeri imk tiksliai kaip dokumente (jei jo nera — palik tuscia).\n"
    "Nekurk poziciju, kuriu dokumente nera."
)


def skaityti(kelias: str) -> list[dict]:
    if not os.path.exists(kelias):
        return []
    try:
        with open(kelias, encoding="utf-8") as f:
            return json.load(f).get("sutartys") or []
    except Exception:
        return []


def _norm(nr: str) -> str:
    return (nr or "").replace(" ", "").upper()


def issaugoti_sutarti(kelias: str, sutartis: dict) -> None:
    """Prideda arba ATNAUJINA sutarti pagal numeri (upsert)."""
    visos = skaityti(kelias)
    sutartis = dict(sutartis)
    sutartis["atnaujinta"] = datetime.now().isoformat(timespec="seconds")
    visos = [s for s in visos if _norm(s.get("numeris")) != _norm(sutartis.get("numeris"))]
    visos.append(sutartis)
    visos.sort(key=lambda s: s.get("numeris") or "")
    laik = kelias + ".tmp"
    with open(laik, "w", encoding="utf-8") as f:
        json.dump({"sutartys": visos}, f, ensure_ascii=False, indent=1)
    os.replace(laik, kelias)


def rasti(kelias: str, numeris: str) -> dict | None:
    for s in skaityti(kelias):
        if _norm(s.get("numeris")) == _norm(numeris):
            return s
    return None


def istraukti_ikainius(baitai: bytes, mime: str) -> dict:
    """AI istraukia sutartinius ikainius is sutarties/pasiulymo failo.
    Grazina {"sutarties_numeris", "tiekejas", "pozicijos", "_ai"}."""
    gabalai = ekstrakcija._siuntimo_gabalai(baitai, mime)
    if ekstrakcija.MODELIS.startswith("gemini"):
        import base64
        dalys = [{"inline_data": {"mime_type": m, "data": base64.standard_b64encode(b).decode()}}
                 for m, b in gabalai]
        dalys.append({"text": PROMPTAS})
        ats = ekstrakcija._gemini_post(ekstrakcija.MODELIS, dalys, IRANKIS["input_schema"], 30000)
        tekstas = ekstrakcija._gemini_tekstas(ats)
        if not tekstas.strip():
            raise RuntimeError("Gemini grazino tuscia atsakyma")
        duomenys = json.loads(tekstas)
        naudota = ekstrakcija._gemini_naudota(ats)
        duomenys["_ai"] = {
            "modelis": ats.get("modelVersion") or ekstrakcija.MODELIS,
            "tokenai_in": naudota["tokenai_in"], "tokenai_out": naudota["tokenai_out"],
            "kaina_ct": ekstrakcija.kaina_ct(ekstrakcija.MODELIS, naudota["tokenai_in"], naudota["tokenai_out"]),
        }
        return duomenys

    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    turinys = [ekstrakcija._blokas(b, m) for m, b in gabalai]
    turinys.append({"type": "text", "text": PROMPTAS})
    with client.messages.stream(
        model=ekstrakcija.MODELIS,
        max_tokens=30000,
        messages=[{"role": "user", "content": turinys}],
        tools=[IRANKIS],
        tool_choice={"type": "tool", "name": "pateikti_ikainius"},
    ) as srautas:
        msg = srautas.get_final_message()
    duomenys = None
    for blokas in msg.content:
        if blokas.type == "tool_use":
            duomenys = blokas.input
            break
    if not duomenys:
        raise RuntimeError("AI nepateike ikainiu")
    duomenys["_ai"] = {
        "modelis": ekstrakcija.MODELIS,
        "tokenai_in": msg.usage.input_tokens, "tokenai_out": msg.usage.output_tokens,
        "kaina_ct": ekstrakcija.kaina_ct(ekstrakcija.MODELIS, msg.usage.input_tokens, msg.usage.output_tokens),
    }
    return duomenys

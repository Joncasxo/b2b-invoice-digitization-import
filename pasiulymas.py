"""
PASIULYMO/SUTARTIES EILUCIU SUSIEJIMAS su saskaitos eilutemis.

UNIVERSALU failo formatui: pozicijos traukiamos is xlsx, docx (lenteles ir
pastraipos), pdf (teksto sluoksnis), csv, txt — visur ta pati taisykle:
eilute = tekstas (pavadinimas) + bent vienas skaicius toje pacioje eiluteje.
Jokiu formatu sablonu ar stulpeliu spejimo — skaiciai tiesiog surenkami,
o ju prasme (kaina/kiekis/vienetas) nustatoma susiejimo metu.

Sluoksniai (patikrinta ant tikro atvejo KAA36426, 2026-08-28):
  1) TIKSLUS KAINOS sutapimas — grynas kodas, 0 ct;
  2) likusioms — AI (pigus Gemini Flash, ~0.05 ct): vardai dokumentuose
     skiriasi per daug, kad uztektu zodziu palyginimo (saknys: 1/6, AI: 6/6).
     AI kartu grazina SUTARTA kaina/kieki/vieneta is pasiulymo pozicijos —
     jie rodomi apskaitai prie saskaitos eilutes (sutarta vs faktura).
Neaiskios lieka NESUSIETOS; zmogus pataiso UI (select/drag), jo zodis virsesnis.
"""

import json
import re

PALAIKOMI = (".xlsx", ".docx", ".pdf", ".csv", ".txt")


def _skaicius(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    s = str(v).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return round(float(s), 4)


def _skaiciai_tekste(t) -> set:
    return {round(float(m.replace(",", ".")), 4)
            for m in re.findall(r"\d+(?:[.,]\d+)?", str(t or ""))}


def _kandidatai(eilutes_zaliavos) -> list[dict]:
    """[(tekstai[], skaiciai set, eilutes_tekstas, tvirta)] -> pozicijos
    {nr, pavadinimas, skaiciai, eilute}. `eilute` — VISA eilute su langeliais,
    kad AI matytu konteksta. `tvirta` — ar eilute panasi i TIKRA pozicija
    (Excel: yra skaiciaus TIPO langelis; kiti formatai: bent 2 skaiciai) —
    kitaip antrastes/datos/pastabos taptu "pozicijomis" ir siukslintu sarasa."""
    rez = []
    nr = 0
    for tekstai, skaiciai, eilutes_tekstas, tvirta in eilutes_zaliavos:
        tekstai = [t for t in tekstai if len(t.strip()) >= 4]
        if not tekstai or not skaiciai or not tvirta:
            continue
        nr += 1
        rez.append({"nr": nr, "pavadinimas": max(tekstai, key=len).strip()[:200],
                    "skaiciai": sorted(skaiciai)[:12],
                    "eilute": str(eilutes_tekstas).strip()[:300]})
        if len(rez) >= 200:
            break
    return rez


def _is_xlsx(kelias: str):
    from openpyxl import load_workbook
    wb = load_workbook(kelias, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:5]:
        for r in ws.iter_rows(max_row=400, max_col=40, values_only=True):
            tekstai = []
            skaiciai = set()
            tvirta = False   # bent vienas SKAICIAUS TIPO langelis = tikra pozicija
            for c in r:
                s = _skaicius(c)
                if s is not None:
                    skaiciai.add(s)
                    if not isinstance(c, str):
                        tvirta = True
                elif isinstance(c, str) and c.strip():
                    tekstai.append(c.strip())
                    if len(c) < 40:   # "12,50 €" tipo langeliai; ilgu tekstu skaiciu nerankiojam
                        skaiciai |= _skaiciai_tekste(c)
            out.append((tekstai, skaiciai, " | ".join(str(c).strip() for c in r if c is not None and str(c).strip()), tvirta))
    wb.close()
    return out


def _is_docx(kelias: str):
    import docx
    dok = docx.Document(kelias)
    out = []
    for lentele in dok.tables:
        for row in lentele.rows:
            celes = [c.text.strip() for c in row.cells]
            skaiciai = set().union(*[_skaiciai_tekste(t) for t in celes]) if celes else set()
            out.append((celes, skaiciai, " | ".join(t for t in celes if t), len(skaiciai) >= 2))
    for p in dok.paragraphs:
        t = p.text.strip()
        if t:
            sk = _skaiciai_tekste(t)
            out.append(([t], sk, t, len(sk) >= 2))
    return out


def _is_pdf(kelias: str):
    from pypdf import PdfReader
    out = []
    try:
        r = PdfReader(kelias)
    except Exception:
        return out
    for lapas in r.pages[:20]:
        for eil in (lapas.extract_text() or "").splitlines():
            eil = eil.strip()
            if eil:
                sk = _skaiciai_tekste(eil)
                out.append(([eil], sk, eil, len(sk) >= 2))
    return out


def _is_teksto(kelias: str):
    out = []
    with open(kelias, encoding="utf-8", errors="replace") as f:
        for eil in f.read(300_000).splitlines():
            eil = eil.strip()
            if eil:
                dalys = re.split(r"[;,|\t]", eil)
                sk = _skaiciai_tekste(eil)
                out.append(([d.strip() for d in dalys], sk, eil, len(sk) >= 2))
    return out


def eilutes_is_priedo(kelias: str) -> list[dict]:
    """Kandidatines pozicijos is BET KOKIO palaikomo priedo formato."""
    ext = ("." + kelias.rsplit(".", 1)[-1]).lower() if "." in kelias else ""
    try:
        if ext == ".xlsx":
            return _kandidatai(_is_xlsx(kelias))
        if ext == ".docx":
            return _kandidatai(_is_docx(kelias))
        if ext == ".pdf":
            return _kandidatai(_is_pdf(kelias))
        if ext in (".csv", ".txt"):
            return _kandidatai(_is_teksto(kelias))
    except Exception as e:
        print(f"[pasiulymas] {ext} skaitymas nepavyko: {e}", flush=True)
    return []


# senas vardas — suderinamumui su esamais skriptais
def eilutes_is_xlsx(kelias: str, daugiausiai: int = 200) -> list[dict]:
    return eilutes_is_priedo(kelias)


def _susieti_ai(liko: list[tuple[int, dict]], laisvos_poz: list[dict]) -> dict[int, dict]:
    """AI sluoksnis. Grazina {saskaitos_indeksas: {pasiulymo_nr, kaina, kiekis, vienetas}}."""
    import ekstrakcija
    se = [{"eil": i + 1, "pavadinimas": e.get("pavadinimas"), "kiekis": e.get("kiekis"),
           "vienetas": e.get("vienetas"), "vnt_kaina": e.get("vnt_kaina")}
          for i, (_, e) in enumerate(liko)]
    # AI gauna VISA pozicijos eilute (langeliai su kontekstu) — kad kaina/kiekis/
    # vienetas butu imami is TOS pozicijos, ne spejami is pliku skaiciu
    pas = [{"nr": p["nr"], "pozicija": p.get("eilute") or p["pavadinimas"]}
           for p in laisvos_poz]
    schema = {"type": "object", "properties": {"sasajos": {"type": "array", "items": {
        "type": "object", "properties": {
            "eil": {"type": "integer"},
            "pasiulymo_nr": {"type": "integer", "nullable": True},
            "sutarta_kaina": {"type": "number", "nullable": True},
            "sutartas_kiekis": {"type": "number", "nullable": True},
            "vienetas": {"type": "string", "nullable": True},
        }, "required": ["eil", "pasiulymo_nr", "sutarta_kaina", "sutartas_kiekis", "vienetas"]}}},
        "required": ["sasajos"]}
    promptas = (
        "Pirkejas gavo tiekejo PASIULYMA (ar sutarti) ir pagal ji israsyta SASKAITA. "
        "Pavadinimai dokumentuose gali skirtis — lygink pagal prasme, kiekius ir kainas. "
        "Kiekvienai saskaitos eilutei nurodyk atitinkancia pasiulymo pozicija (pasiulymo_nr; "
        "null jei neatitinka ne vienos; kiekviena pozicija naudojama daugiausiai karta) ir "
        "IS PASIULYMO pozicijos: sutarta VIENETO kaina (sutarta_kaina), sutarta kieki "
        "(sutartas_kiekis) ir mato vieneta (vienetas) — null, kai nesimato.\n"
        "SVARBU: siek TIK su pozicija, kuri yra preke ar paslauga; adresu, datu, "
        "antrasciu, tarpiniu sumu eiluciu NESIEK. Kai abejoji — null: tuscia sasaja "
        "geriau nei klaidinga.\n\n"
        "SASKAITOS EILUTES:\n" + json.dumps(se, ensure_ascii=False) +
        "\n\nPASIULYMO POZICIJOS:\n" + json.dumps(pas, ensure_ascii=False))
    # limitas su atsarga: Flash "mastymo" tokenai skaiciuojasi i isvesti
    ats = ekstrakcija._gemini_post(ekstrakcija.MODELIS, [{"text": promptas}], schema, 8000)
    rez = json.loads(ekstrakcija._gemini_tekstas(ats))
    nauda = ekstrakcija._gemini_naudota(ats)
    print(f"[pasiulymas] AI susiejimas: {len(se)} eil., tokenai {nauda}", flush=True)

    galimi_nr = {p["nr"] for p in laisvos_poz}
    out: dict[int, dict] = {}
    panaudoti: set[int] = set()
    for s in rez.get("sasajos") or []:
        eil = s.get("eil")
        pnr = s.get("pasiulymo_nr")
        if not isinstance(eil, int) or not (1 <= eil <= len(liko)):
            continue
        if pnr is None or pnr not in galimi_nr or pnr in panaudoti:
            continue
        out[liko[eil - 1][0]] = {"pasiulymo_nr": pnr, "kaina": s.get("sutarta_kaina"),
                                 "kiekis": s.get("sutartas_kiekis"), "vienetas": s.get("vienetas")}
        panaudoti.add(pnr)
    return out


def susieti(saskaitos_eilutes: list[dict], pasiulymo_eilutes: list[dict]) -> list[dict | None]:
    """Kiekvienai saskaitos eilutei — None arba
    {indeksas, kaina, kiekis, vienetas} (sutartos reiksmes is pasiulymo)."""
    laisvos = set(range(len(pasiulymo_eilutes)))
    rez: list[dict | None] = [None] * len(saskaitos_eilutes)

    # 1 sluoksnis: TIKSLI kaina (kiekio sutapimas persveria prie lygiuju)
    for i, e in enumerate(saskaitos_eilutes):
        kaina = _skaicius(e.get("vnt_kaina"))
        kiekis = _skaicius(e.get("kiekis"))
        if kaina is None:
            continue
        geriausia = None
        for j in sorted(laisvos):
            sk = pasiulymo_eilutes[j]["skaiciai"]
            if kaina not in sk:
                continue
            balas = 1 + (kiekis is not None and kiekis in sk)
            if geriausia is None or balas > geriausia[0]:
                geriausia = (balas, j)
        if geriausia:
            j = geriausia[1]
            rez[i] = {"indeksas": j, "kaina": kaina,
                      "kiekis": kiekis if (kiekis is not None and kiekis in pasiulymo_eilutes[j]["skaiciai"]) else None,
                      "vienetas": None}
            laisvos.discard(j)

    # 2 sluoksnis: AI likusioms (tik su pavadinimu)
    liko = [(i, saskaitos_eilutes[i]) for i, r in enumerate(rez)
            if r is None and str(saskaitos_eilutes[i].get("pavadinimas") or "").strip()]
    if liko and laisvos:
        nr_i_indeksa = {pasiulymo_eilutes[j]["nr"]: j for j in laisvos}
        try:
            ai = _susieti_ai(liko, [pasiulymo_eilutes[j] for j in sorted(laisvos)])
            for i, s in ai.items():
                j = nr_i_indeksa.get(s["pasiulymo_nr"])
                if j is not None and j in laisvos:
                    rez[i] = {"indeksas": j, "kaina": s.get("kaina"),
                              "kiekis": s.get("kiekis"), "vienetas": s.get("vienetas")}
                    laisvos.discard(j)
        except Exception as e:
            print(f"[pasiulymas] AI sluoksnis nepavyko (lieka be sasaju): {e}", flush=True)

    return rez

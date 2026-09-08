"""
PRIEDU PERZIURA — laisko priedas (Excel/CSV/Word/tekstas) parodomas programos
lange TAIP, kaip atrodo originale, NIEKO nesiunciant i apskaitininkes kompiuteri.

Excel atkuriamas istikimai: sulieti langeliai (colspan/rowspan), tik realiai
uzpildytas plotas, paryskinimai, tekstas lauzosi. HTML surenkamas cia (serveryje),
langeliu tekstas escapinamas — narsyklei lieka tik irodyti.
"""

import html
import os

MAX_EIL = 400          # daugiausia eiluciu viename lape
MAX_STULP = 60         # daugiausia stulpeliu
MAX_TEKSTAS = 200_000  # tekstiniu failu riba (simboliais)
MAX_FAILAS_MB = 5      # didesniu neanalizuojam (priedai buna deshimtys KB)


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _xlsx(kelias: str) -> dict:
    try:
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
    except ImportError:
        return {"tipas": "neparodoma", "zinute": "Serveryje nera Excel skaitytuvo (openpyxl)."}
    try:
        # ne read_only — reikia suliejimu (merged cells) ir sriftu
        wb = load_workbook(kelias, data_only=True)
    except Exception as e:
        return {"tipas": "neparodoma", "zinute": f"Nepavyko perskaityti Excel failo: {e}"}

    lapai = []
    for ws in wb.worksheets[:5]:
        max_r = min(ws.max_row or 1, MAX_EIL)
        max_c = min(ws.max_column or 1, MAX_STULP)

        # Realiai uzpildytas plotas (max_column daznai buna isputstas formatavimo)
        tikras_c = 1
        tikras_r = 1
        for r in ws.iter_rows(min_row=1, max_row=max_r, max_col=max_c):
            for c in r:
                if c.value not in (None, ""):
                    tikras_c = max(tikras_c, c.column)
                    tikras_r = max(tikras_r, c.row)
        # suliejimai gali siekti toliau nei reiksmes
        dengiami = set()      # (eil, stulp) uzdengti suliejimu — ju <td> nededam
        spanai = {}           # (eil, stulp) virsutinis kairys -> (rowspan, colspan)
        for rng in ws.merged_cells.ranges:
            if rng.min_row > max_r or rng.min_col > MAX_STULP:
                continue
            tikras_c = max(tikras_c, min(rng.max_col, MAX_STULP))
            tikras_r = max(tikras_r, min(rng.max_row, max_r))
            spanai[(rng.min_row, rng.min_col)] = (min(rng.max_row, max_r) - rng.min_row + 1,
                                                  min(rng.max_col, MAX_STULP) - rng.min_col + 1)
            for rr in range(rng.min_row, min(rng.max_row, max_r) + 1):
                for cc in range(rng.min_col, min(rng.max_col, MAX_STULP) + 1):
                    if (rr, cc) != (rng.min_row, rng.min_col):
                        dengiami.add((rr, cc))

        if tikras_r == 1 and tikras_c == 1 and ws.cell(1, 1).value in (None, ""):
            continue   # tuscias lapas

        # Stulpeliu plociai is Excel (Excel vienetas ~ 7px)
        cols = []
        for ci in range(1, tikras_c + 1):
            dim = ws.column_dimensions.get(get_column_letter(ci))
            w = getattr(dim, "width", None) if dim else None
            cols.append(f'<col style="width:{int(w * 7)}px">' if w else "<col>")

        eil_html = []
        for ri in range(1, tikras_r + 1):
            td = []
            for ci in range(1, tikras_c + 1):
                if (ri, ci) in dengiami:
                    continue
                c = ws.cell(ri, ci)
                span = spanai.get((ri, ci))
                extra = ""
                if span:
                    if span[0] > 1:
                        extra += f' rowspan="{span[0]}"'
                    if span[1] > 1:
                        extra += f' colspan="{span[1]}"'
                stil = ""
                if getattr(getattr(c, "font", None), "bold", False):
                    stil = ' style="font-weight:700"'
                reiksme = _esc(c.value)
                td.append(f"<td{extra}{stil}>{reiksme}</td>")
            eil_html.append("<tr>" + "".join(td) + "</tr>")

        lapai.append({
            "vardas": ws.title,
            "html": f'<table class="xl"><colgroup>{"".join(cols)}</colgroup>{"".join(eil_html)}</table>',
        })
    wb.close()
    if not lapai:
        return {"tipas": "neparodoma", "zinute": "Excel failas tuscias."}
    return {"tipas": "lapai", "lapai": lapai}


def _csv(kelias: str) -> dict:
    import csv as _csv
    with open(kelias, encoding="utf-8", errors="replace") as f:
        turinys = f.read(MAX_TEKSTAS)
    pirma = (turinys.splitlines() or [""])[0]
    skirtukas = max(";,|\t", key=pirma.count) if any(s in pirma for s in ";,|\t") else ";"
    eil = []
    for r in _csv.reader(turinys.splitlines()[:MAX_EIL], delimiter=skirtukas):
        eil.append("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r[:MAX_STULP]) + "</tr>")
    return {"tipas": "lapai", "lapai": [{"vardas": "", "html": f'<table class="xl">{"".join(eil)}</table>'}]}


def _docx(kelias: str) -> dict:
    try:
        import docx
    except ImportError:
        return {"tipas": "neparodoma", "zinute": "Serveryje nera Word skaitytuvo (python-docx)."}
    try:
        dok = docx.Document(kelias)
    except Exception as e:
        return {"tipas": "neparodoma", "zinute": f"Nepavyko perskaityti Word failo: {e}"}
    # Word pozicijos beveik visada guli LENTELESE — pastraipu sarase ju nera,
    # todel lenteles atvaizduojamos atskirai (kaip Excel), tekstas — savo vietoje
    dalys = []
    tekstas = "\n".join(p.text for p in dok.paragraphs if p.text.strip())[:MAX_TEKSTAS]
    if tekstas:
        dalys.append(f'<pre style="white-space:pre-wrap; font:inherit; margin:0 0 10px">{_esc(tekstas)}</pre>')
    for lentele in dok.tables[:10]:
        eil = []
        for row in lentele.rows[:MAX_EIL]:
            eil.append("<tr>" + "".join(f"<td>{_esc(c.text)}</td>" for c in row.cells[:MAX_STULP]) + "</tr>")
        if eil:
            dalys.append(f'<table class="xl">{"".join(eil)}</table>')
    if not dalys:
        return {"tipas": "neparodoma", "zinute": "Word failas tuscias."}
    return {"tipas": "lapai", "lapai": [{"vardas": "", "html": "<div style='margin-bottom:8px'></div>".join(dalys)}]}


def perziura(kelias: str) -> dict:
    """{tipas: lapai|tekstas|neparodoma, ...} — UI modalui."""
    if os.path.getsize(kelias) > MAX_FAILAS_MB * 1024 * 1024:
        return {"tipas": "neparodoma", "zinute": f"Failas didesnis nei {MAX_FAILAS_MB} MB — perziura negalima."}
    ext = os.path.splitext(kelias)[1].lower()
    if ext == ".xlsx":
        return _xlsx(kelias)
    if ext == ".csv":
        return _csv(kelias)
    if ext == ".txt":
        with open(kelias, encoding="utf-8", errors="replace") as f:
            return {"tipas": "tekstas", "tekstas": f.read(MAX_TEKSTAS)}
    if ext == ".docx":
        return _docx(kelias)
    return {"tipas": "neparodoma",
            "zinute": f"Šio formato ({ext}) peržiūra nepalaikoma — paprašyk siuntėjo .xlsx ar .csv."}

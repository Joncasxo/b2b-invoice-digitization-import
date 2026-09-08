"""
XML generavimas — GRYNAS KODAS (0 tokenu). Struktura 1:1 pagal pilna pavyzdini
Document-Invoice faila (Fitek EDI, pavyzdine tiekejo saskaita).

Pastabos is pavyzdzio analizes:
  - ILN = "998" + imones kodas (9 skaitm.) + GS1 kontrolinis skaitmuo
    (pirkejo ILN pavyzdyje tiksliai atitinka; tiekejo pavyzdyje buvo TIKRAS
    registruotas GLN ...7000, kurio nezinom — generuojam pagal formule).
  - PVM suvestineje skaiciuojamas nuo BENDROS sumos pagal tarifa (234.79*21%=49.31),
    ne sumuojant po eilute (gautusi 49.30).
  - Tax-Summary pavyzdyje: TaxRate=0 (nors eilutese 21) ir TaxableAmount = suma SU PVM.
    Atkartota lygiai taip.
  - Tusti laukai: <OriginalFileName></OriginalFileName> (porinis) ir <AccountNumber />
    (savaime uzsidarantis) — kaip pavyzdyje.
"""

import re
from datetime import datetime
from xml.sax.saxutils import escape

# Konstantos is pavyzdzio
DOCUMENT_LINK = "https://edi.fitek.com"
DOCUMENT_SOURCE = "S"
DOCUMENT_TYPE = "INVOICE"
FUNKCIJOS_KODAS = "9"     # EDI: 9 = originali saskaita
PAVADINIMO_KODAS = "380"  # EDI: 380 = komercine saskaita
SALIS = "LT"


def _t(v) -> str:
    return escape(str(v).strip()) if v is not None else ""


def _kodas(v) -> str:
    """Prekes kodas importui — SUTRAUKTAS: be tarpu ir bruksniu, kaip ir saskaitos
    numeris (0715 234 090 -> 0715234090). Tiekejai ta pati koda rasineja skirtingai,
    o Pragmos paieska lygina tekstus tiksliai."""
    return re.sub(r"[\s\-]+", "", str(v or "")).strip()


def _kiekis(v) -> str:
    v = float(v or 0)
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _kaina4(v) -> str:
    return f"{float(v or 0):.4f}"


def _suma2(v) -> str:
    return f"{float(v or 0):.2f}"


def _tarifas(v) -> str:
    v = float(v or 0)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _gs1_kontrolinis(base: str) -> str:
    s = 0
    for i, ch in enumerate(reversed(base)):
        s += int(ch) * (3 if i % 2 == 0 else 1)
    return str((10 - s % 10) % 10)


def _iln(imones_kodas: str) -> str:
    """ILN/GLN: 998 + imones kodas + kontrolinis (pagal pavyzdzio pirkejo ILN)."""
    k = re.sub(r"\D", "", str(imones_kodas or ""))
    if len(k) != 9:
        return ""
    base = "998" + k
    return base + _gs1_kontrolinis(base)


def generuoti_xml(d: dict, dabar: datetime | None = None) -> str:
    """
    d = {
      tiekejas: {pavadinimas, imones_kodas, pvm_kodas, gatve, miestas, pasto_kodas},
      pirkejas: {pavadinimas, imones_kodas, pvm_kodas, miestas},
      saskaitos_numeris, saskaitos_data, apmoketi_iki, valiuta,
      eilutes: [{pavadinimas, ean, tiekejo_kodas, pirkejo_kodas (pr...),
                 pirkejo_pavadinimas (parinktos/kuriamos korteles vardas),
                 kiekis, vienetas, vnt_kaina, suma, pvm_proc}],
    }
    """
    tie = d.get("tiekejas") or {}
    pir = d.get("pirkejas") or {}
    eilutes = d.get("eilutes") or []

    tie_iln = _iln(tie.get("imones_kodas"))
    pir_iln = _iln(pir.get("imones_kodas"))
    data = _t(d.get("saskaitos_data"))
    # DocumentReceiveDateTime = SASKAITOS data (užsakovo patikslinimas: dokumento data
    # visada lygi saskaitos datai, gavimo laikas nieko nelemia — KONTEKSTAS_KITAM_CLAUDE.md)
    if data:
        dt = f"{data}T00:00:00"
    else:
        dt = (dabar or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    # Numeris apskaitai — sutrauktas (be tarpu ir bruksniu), kaip pavyzdyje MKV0198097
    numeris = re.sub(r"[\s\-]+", "", str(d.get("saskaitos_numeris") or ""))

    x = []
    x.append('<?xml version="1.0" encoding="UTF-8"?>')
    x.append("<Document-Invoice>")

    # ── <Pragma>: ka daryti su saskaita Pragmoje (importo lango pasirinkimai) ──
    # Rankinis importo langas sios sekcijos NESKAITO (netrukdo), auto rezimas —
    # skaito. Reiksmes TIK is Pragmos konteksto sarasu (tikrinama server.py).
    # <KurtiKorteles>/<KurtiTiekejus> nebesiunciami — auto rezime kuriama visada.
    pr = d.get("pragma") or {}
    if pr.get("duombaze"):
        x.append("  <Pragma>")
        x.append(f"    <Duombaze>{_t(pr.get('duombaze'))}</Duombaze>")
        x.append(f"    <Sandelis>{_t(pr.get('sandelis'))}</Sandelis>")
        x.append(f"    <Tipas>{_t(pr.get('tipas'))}</Tipas>")
        x.append(f"    <Statusas>{_t(pr.get('statusas'))}</Statusas>")
        x.append(f"    <SavaImone>{_t(pr.get('sava_imone'))}</SavaImone>")
        x.append(f"    <PrekesGrupe>{_t(pr.get('grupe') or '**')}</PrekesGrupe>")
        x.append(f"    <Kainorastis>{_t(pr.get('kainorastis'))}</Kainorastis>")
        x.append("  </Pragma>")

    # ── Document-Header ────────────────────────────────────────────────────
    x.append("  <Document-Header>")
    x.append(f"    <DocumentReceiveDateTime>{dt}</DocumentReceiveDateTime>")
    x.append(f"    <DocumentLink>{DOCUMENT_LINK}</DocumentLink>")
    x.append(f"    <DocumentType>{DOCUMENT_TYPE}</DocumentType>")
    x.append(f"    <DocumentSource>{DOCUMENT_SOURCE}</DocumentSource>")
    x.append("    <OriginalFileName></OriginalFileName>")
    x.append("  </Document-Header>")

    # ── Invoice-Header ─────────────────────────────────────────────────────
    x.append("  <Invoice-Header>")
    x.append(f"    <InvoiceNumber>{_t(numeris)}</InvoiceNumber>")
    x.append(f"    <InvoiceDate>{data}</InvoiceDate>")
    x.append(f"    <InvoiceCurrency>{_t(d.get('valiuta') or 'EUR')}</InvoiceCurrency>")
    x.append(f"    <InvoicePaymentDueDate>{_t(d.get('apmoketi_iki'))}</InvoicePaymentDueDate>")
    x.append(f"    <InvoicePostDate>{data}</InvoicePostDate>")
    x.append(f"    <DocumentFunctionCode>{FUNKCIJOS_KODAS}</DocumentFunctionCode>")
    x.append(f"    <DocumentNameCode>{PAVADINIMO_KODAS}</DocumentNameCode>")
    x.append("  </Invoice-Header>")

    # ── Document-Parties (siuntejas = tiekejas, gavejas = pirkejas) ────────
    x.append("  <Document-Parties>")
    x.append("    <Sender>")
    x.append(f"      <ILN>{tie_iln}</ILN>")
    x.append(f"      <CodeBySender>{_t(tie.get('imones_kodas'))}</CodeBySender>")
    x.append(f"      <Name>{_t(tie.get('pavadinimas'))}</Name>")
    x.append("    </Sender>")
    x.append("    <Receiver>")
    x.append(f"      <ILN>{pir_iln}</ILN>")
    x.append(f"      <CodeBySender>{_t(pir.get('imones_kodas'))}</CodeBySender>")
    x.append(f"      <Name>{_t(pir.get('pavadinimas'))}</Name>")
    x.append("    </Receiver>")
    x.append("  </Document-Parties>")

    # ── Invoice-Parties (Buyer / Payer / Seller — lauku tvarka kaip pavyzdyje) ──
    x.append("  <Invoice-Parties>")
    x.append("    <Buyer>")
    x.append(f"      <ILN>{pir_iln}</ILN>")
    x.append(f"      <TaxID>{_t(pir.get('pvm_kodas'))}</TaxID>")
    x.append(f"      <CodeBySeller>{_t(pir.get('imones_kodas'))}</CodeBySeller>")
    x.append(f"      <UtilizationRegisterNumber>{_t(pir.get('imones_kodas'))}</UtilizationRegisterNumber>")
    x.append(f"      <Name>{_t(pir.get('pavadinimas'))}</Name>")
    x.append(f"      <CityName>{_t(pir.get('miestas'))}</CityName>")
    x.append("    </Buyer>")
    x.append("    <Payer>")
    x.append(f"      <ILN>{pir_iln}</ILN>")
    x.append(f"      <TaxID>{_t(pir.get('pvm_kodas'))}</TaxID>")
    x.append(f"      <CodeBySeller>{_t(pir.get('imones_kodas'))}</CodeBySeller>")
    x.append(f"      <UtilizationRegisterNumber>{_t(pir.get('imones_kodas'))}</UtilizationRegisterNumber>")
    x.append(f"      <Name>{_t(pir.get('pavadinimas'))}</Name>")
    x.append(f"      <CityName>{_t(pir.get('miestas'))}</CityName>")
    x.append(f"      <Country>{SALIS}</Country>")
    x.append("    </Payer>")
    x.append("    <Seller>")
    x.append(f"      <ILN>{tie_iln}</ILN>")
    x.append(f"      <TaxID>{_t(tie.get('pvm_kodas'))}</TaxID>")
    x.append(f"      <CodeBySeller>{_t(tie.get('imones_kodas'))}</CodeBySeller>")
    x.append(f"      <UtilizationRegisterNumber>{_t(tie.get('imones_kodas'))}</UtilizationRegisterNumber>")
    x.append(f"      <Name>{_t(tie.get('pavadinimas'))}</Name>")
    x.append(f"      <StreetAndNumber>{_t(tie.get('gatve'))}</StreetAndNumber>")
    x.append(f"      <CityName>{_t(tie.get('miestas'))}</CityName>")
    x.append(f"      <PostalCode>{_t(tie.get('pasto_kodas'))}</PostalCode>")
    x.append(f"      <Country>{SALIS}</Country>")
    x.append("      <AccountNumber />")
    x.append("    </Seller>")
    x.append("  </Invoice-Parties>")

    # ── Eilutes — 1:1 pagal pavyzdi (veikia su bet kokiu eiluciu skaiciumi) ──
    x.append("  <Invoice-Lines>")
    for nr, e in enumerate(eilutes, 1):
        ean = _kodas(e.get("ean"))
        tiek_kodas = _kodas(e.get("tiekejo_kodas"))
        pirk_kodas = _kodas(e.get("pirkejo_kodas"))  # pasirinktos korteles pr kodas

        # Kai kortele PARINKTA — pr kodas VISUOSE trijuose laukuose (užsakovo patikslinimas:
        # importas skirtingu supplier/buyer kodu nepalaiko, o EAN tikrinamas pirmas —
        # pr kodas visur garantuoja, kad importas ras butent parinkta Pragmos preke).
        #
        # Kai kuriama NAUJA kortele — VISI kodu laukai TUSTI. Pragmos importas tada
        # nieko neranda pagal koda/EAN ir sukuria nauja kortele, numeruodamas ja PATS
        # is eiles. Jei paliktume tiekejo koda ar EAN, Pragma juos panaudotu vietoj
        # savo eilinio numerio — todel butent cia jie isvalomi.
        #
        # Kai neparinkta (tik atsisiunciama, be Pragmos kurimo) — tiekejo kodai kaip
        # saskaitoje (pagal pavyzdi: kai nera atskiru kodu, visur naudojamas tas pats).
        if pirk_kodas:
            ean_xml = tiek_xml = pirk_xml = pirk_kodas
        elif e.get("nauja_kortele"):
            ean_xml = tiek_xml = pirk_xml = ""
        else:
            ean_xml = ean or tiek_kodas
            tiek_xml = tiek_kodas or ean
            pirk_xml = ean or tiek_kodas

        x.append("    <Line>")
        x.append("      <Line-Item>")
        x.append(f"        <LineNumber>{nr}</LineNumber>")
        x.append(f"        <EAN>{_t(ean_xml)}</EAN>")
        x.append(f"        <BuyerItemCode>{_t(pirk_xml)}</BuyerItemCode>")
        x.append(f"        <SupplierItemCode>{_t(tiek_xml)}</SupplierItemCode>")
        # PAVADINIMAS: kai kortele parinkta arba kuriama — MUSU korteles vardas,
        # ne tiekejo uzrasas. Priezastis: is sito lauko Pragma kuria trukstamas
        # prekiu korteles, tad kitaip naujos kortelės vardas ateitu is saskaitos.
        # Kai kortele neparinkta — lieka tiekejo uzrasas (nieko kito nera).
        pav_xml = (e.get("pirkejo_pavadinimas") or "").strip() or e.get("pavadinimas")
        x.append(f"        <ItemDescription>{_t(pav_xml)}</ItemDescription>")
        x.append(f"        <InvoiceQuantity>{_kiekis(e.get('kiekis'))}</InvoiceQuantity>")
        x.append(f"        <InvoiceUnitNetPrice>{_kaina4(e.get('vnt_kaina'))}</InvoiceUnitNetPrice>")
        x.append(f"        <UnitOfMeasure>{_t(e.get('vienetas') or 'VNT.')}</UnitOfMeasure>")
        x.append(f"        <TaxRate>{_tarifas(e.get('pvm_proc'))}</TaxRate>")
        x.append("        <TaxCategoryCode>S</TaxCategoryCode>")
        x.append(f"        <NetAmount>{_suma2(e.get('suma'))}</NetAmount>")
        # Eilutes PVM suma (apskaitininkes pakoreguota centu tikslumu). Pragmos
        # importas siandien ja ignoruoja (skaiciuoja pats), bet skaitys, kai
        # Pragmos agentas ijungs <TaxAmount> (2026-09-06 susitarimas) — siunciam jau dabar
        if e.get("pvm_suma") is not None and e.get("pvm_suma") != "":
            x.append(f"        <TaxAmount>{_suma2(e.get('pvm_suma'))}</TaxAmount>")
        x.append("      </Line-Item>")
        x.append("      <Line-Reference />")
        x.append("    </Line>")
    x.append("  </Invoice-Lines>")

    # ── Suvestine ───────────────────────────────────────────────────────────
    # PVM: kai eilutes turi PVM sumas (UI stulpelis "PVM €", apskaitininke gali
    # koreguoti centus pagal dokumenta) — suvestine yra JU suma, 2 sk. po kablelio.
    # Kitaip (seni irasai be lauko) — nuo bendros sumos pagal tarifa, kaip pavyzdyje.
    neto = round(sum(float(e.get("suma") or 0) for e in eilutes), 2)
    pvm_eiluciu = [e.get("pvm_suma") for e in eilutes]
    if eilutes and all(v is not None and v != "" for v in pvm_eiluciu):
        pvm = round(sum(round(float(v), 2) for v in pvm_eiluciu), 2)
    else:
        grupes: dict[float, float] = {}
        for e in eilutes:
            r = float(e.get("pvm_proc") or 0)
            grupes[r] = grupes.get(r, 0.0) + float(e.get("suma") or 0)
        pvm = round(sum(round(round(gn, 2) * r / 100, 2) for r, gn in grupes.items()), 2)
    viso = round(neto + pvm, 2)

    x.append("  <Invoice-Summary>")
    x.append(f"    <TotalLines>{len(eilutes)}</TotalLines>")
    x.append(f"    <TotalNetAmount>{_suma2(neto)}</TotalNetAmount>")
    x.append(f"    <TotalTaxAmount>{_suma2(pvm)}</TotalTaxAmount>")
    x.append(f"    <TotalGrossAmount>{_suma2(viso)}</TotalGrossAmount>")
    x.append("    <Tax-Summary>")
    x.append("      <Tax-Summary-Line>")
    x.append("        <TaxRate>0</TaxRate>")  # pavyzdyje butent 0, nors eilutese 21
    x.append("        <TaxCategoryCode>S</TaxCategoryCode>")
    x.append(f"        <TaxAmount>{_suma2(pvm)}</TaxAmount>")
    x.append(f"        <TaxableAmount>{_suma2(viso)}</TaxableAmount>")  # pavyzdyje = SU PVM
    x.append("      </Tax-Summary-Line>")
    x.append("    </Tax-Summary>")
    x.append("  </Invoice-Summary>")

    # ── <Pardavimas>: PORA — ta pati preke iskart parduodama (Pragmos agento
    # formatas 2026-09-05). Eilute nurodo PIRKIMO EILUTES NUMERI (PirkimoEilNr =
    # LineNumber), ne prekes koda — naujos prekes PR kodas gimsta tik importe.
    # Kaina PO nuolaidos be PVM; <Suma> — eilutes suma be PVM (centu tikslumas,
    # musu prasymu, pirmesne uz Kaina x Kiekis). Be numerio (TKL duoda Pragma),
    # be savikainos. Pardavejas ir sandelis — is <Pragma>. Statusas: pirkimas
    # Patvirtintas, pardavimas Nepatvirtintas (Pragma tvirtinant nuraso partijas).
    pard = d.get("pardavimas") or {}
    pard_eil = pard.get("eilutes") or []
    if pard.get("pirkejas") and pard_eil:
        x.append("  <Pardavimas>")
        x.append(f"    <Tipas>{_t(pard.get('tipas'))}</Tipas>")
        x.append(f"    <Pirkejas>{_t(pard.get('pirkejas'))}</Pirkejas>")
        x.append(f"    <Data>{_t(pard.get('data') or data)}</Data>")
        if pard.get("apmoketi_iki"):
            x.append(f"    <ApmokejimoData>{_t(pard.get('apmoketi_iki'))}</ApmokejimoData>")
        if pard.get("projektas"):
            x.append(f"    <Projektas>{_t(pard.get('projektas'))}</Projektas>")
        x.append(f"    <Pastaba>{_t(pard.get('pastaba'))}</Pastaba>")
        x.append("    <Eilutes>")
        for pe in pard_eil:
            x.append("      <Eilute>")
            x.append(f"        <PirkimoEilNr>{int(pe.get('pirkimo_eil_nr'))}</PirkimoEilNr>")
            x.append(f"        <Kiekis>{_kiekis(pe.get('kiekis'))}</Kiekis>")
            x.append(f"        <Kaina>{_kaina4(pe.get('kaina'))}</Kaina>")
            x.append(f"        <Suma>{_suma2(pe.get('suma'))}</Suma>")
            x.append(f"        <PvmProc>{_tarifas(pe.get('pvm_proc'))}</PvmProc>")
            if pe.get("projektas"):
                x.append(f"        <Projektas>{_t(pe.get('projektas'))}</Projektas>")
            x.append("      </Eilute>")
        x.append("    </Eilutes>")
        x.append("  </Pardavimas>")

    x.append("</Document-Invoice>")
    return "\n".join(x) + "\n"


def pirkimo_numeris_xml(saskaitos_numeris) -> str:
    """Tas pats numeris, koks eina i pirkimo <InvoiceNumber> — RAKTAS, kuriuo
    pardavimas rodo i pirkima (Pragma lygina tiksliai, be tarpu/bruksniu)."""
    return re.sub(r"[\s\-]+", "", str(saskaitos_numeris or ""))


def generuoti_pardavimo_xml(d: dict) -> str:
    """
    ATSKIRAS pardavimo dokumentas <Document-Sale> (Pragmos agento instrukcija
    2026-09-06/07, nr.14/16/19): pardavimas surenkamas is VIENO AR KELIU pirkimu,
    kiekviena eilute rodo i pirkimo <InvoiceNumber> + <LineNumber>. Be dokumento
    numerio (TKL duoda Pragma), be statuso (visada nepatvirtintas), be savikainos
    (Pragma ima is patvirtinto pirkimo), be NomNr (kodas gimsta importe).
    d = {
      pragma: {duombaze, sandelis (atsargine reiksme), tipas (PARDAVIMO tipas), sava_imone},
      uzsakymo_nr, data, apmoketi_iki, pirkejas, projektas, pastaba,
      eilutes: [{pirkimo_saskaita, pirkimo_eil_nr, pirkimo_tiekejas, kiekis, kaina, suma, pvm_proc, projektas}],
    }
    """
    pr = d.get("pragma") or {}
    x = []
    x.append('<?xml version="1.0" encoding="UTF-8"?>')
    x.append("<Document-Sale>")
    x.append("  <Pragma>")
    x.append(f"    <Duombaze>{_t(pr.get('duombaze'))}</Duombaze>")
    x.append(f"    <Sandelis>{_t(pr.get('sandelis'))}</Sandelis>")
    x.append(f"    <Tipas>{_t(pr.get('tipas'))}</Tipas>")
    x.append(f"    <SavaImone>{_t(pr.get('sava_imone'))}</SavaImone>")
    x.append("  </Pragma>")
    x.append("  <Sale-Header>")
    x.append(f"    <UzsakymoNr>{_t(d.get('uzsakymo_nr'))}</UzsakymoNr>")
    x.append(f"    <Data>{_t(d.get('data'))}</Data>")
    if d.get("apmoketi_iki"):
        x.append(f"    <ApmokejimoData>{_t(d.get('apmoketi_iki'))}</ApmokejimoData>")
    x.append(f"    <Pirkejas>{_t(d.get('pirkejas'))}</Pirkejas>")
    if d.get("projektas"):
        x.append(f"    <Projektas>{_t(d.get('projektas'))}</Projektas>")
    x.append(f"    <Pastaba>{_t(d.get('pastaba'))}</Pastaba>")
    x.append("  </Sale-Header>")
    x.append("  <Sale-Lines>")
    for e in d.get("eilutes") or []:
        x.append("    <Line>")
        x.append(f"      <PirkimoSaskaita>{_t(e.get('pirkimo_saskaita'))}</PirkimoSaskaita>")
        x.append(f"      <PirkimoEilNr>{int(e.get('pirkimo_eil_nr'))}</PirkimoEilNr>")
        if e.get("pirkimo_tiekejas"):
            x.append(f"      <PirkimoTiekejas>{_t(e.get('pirkimo_tiekejas'))}</PirkimoTiekejas>")
        x.append(f"      <Kiekis>{_kiekis(e.get('kiekis'))}</Kiekis>")
        x.append(f"      <Kaina>{_kaina4(e.get('kaina'))}</Kaina>")
        x.append(f"      <Suma>{_suma2(e.get('suma'))}</Suma>")
        x.append(f"      <PvmProc>{_tarifas(e.get('pvm_proc'))}</PvmProc>")
        if e.get("projektas"):
            x.append(f"      <Projektas>{_t(e.get('projektas'))}</Projektas>")
        x.append("    </Line>")
    x.append("  </Sale-Lines>")
    x.append("</Document-Sale>")
    return "\n".join(x) + "\n"

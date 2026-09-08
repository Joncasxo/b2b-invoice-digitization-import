"""
Katalogo paieskos variklis — pradinio paieska.py (v8) logika, perkelta i moduli.
Balu matematika IDENTISKA originalui: vektoriai (1.00) + zodziu saknys (0.45)
+ skaiciu padengimas (0.30) - konflikto bauda (0.12).
Vienintelis skirtumas: embeddings galima gauti PAKETU (visa saskaita vienu kvietimu).
"""

import os
import re
import json
import math
import unicodedata
from collections import Counter, defaultdict

import numpy as np

MODELIS = "text-embedding-3-large"

# EMBEDDINGU TIEKEJAS (.env EMBEDDING_TIEKEJAS): "openai" (numatyta) arba "gemini".
# SVARBU: katalogas ir uzklausos PRIVALO buti to paties tiekejo — perjungiant
# reikia perskaiciuoto katalogo (KATALOGO_NPZ i atitinkama faila).
GEMINI_MODELIS = "models/gemini-embedding-2"


def _gemini_embeddingai(tekstai):
    """Gemini embeddingai per REST (be papildomu bibliotekus). Iki 100 tekstu
    per kvietima; 429/5xx kartojama su pauze."""
    import time
    import urllib.request

    raktas = os.environ["GEMINI_API_KEY"]
    modelis = os.environ.get("GEMINI_EMBED_MODELIS") or GEMINI_MODELIS
    url = (f"https://generativelanguage.googleapis.com/v1beta/{modelis}"
           f":batchEmbedContents?key={raktas}")
    visi = []
    for a in range(0, len(tekstai), 100):
        dalis = tekstai[a:a + 100]
        kunas = json.dumps({"requests": [
            {"model": modelis, "content": {"parts": [{"text": t}]}} for t in dalis
        ]}).encode("utf-8")
        for bandymas in range(5):
            try:
                r = urllib.request.Request(url, data=kunas,
                                           headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(r, timeout=120) as ats:
                    duom = json.loads(ats.read().decode())
                visi.extend([e["values"] for e in duom["embeddings"]])
                break
            except Exception as e:
                kodas = getattr(e, "code", None)
                if bandymas < 4 and (kodas in (429, 500, 502, 503) or kodas is None):
                    time.sleep(2 ** bandymas * 2)
                    continue
                raise
    return np.array(visi, dtype=np.float32)

# --- SVORIAI (is paieska.py v8) ---
SV_VEKT = 1.00
SV_ZOD = 0.45
SV_SK = 0.30
SV_SK_VNT = 0.30
BAUDA = 0.12
SV_MATO = 0.08   # korteles matas (vnt/dez/pak...) sutampa su saskaitos eilutes vienetu

# Matu normalizavimas — universalus VIENETU zodynas (ne prekiu/tiekeju):
# "DĖŽ." ir "dez" turi buti tas pats matas
_MATU_SINONIMAI = {
    "vnt": "vnt", "unit": "vnt", "pcs": "vnt", "sht": "vnt", "st": "vnt",
    "dez": "dez", "deze": "dez", "dezute": "dez", "box": "dez",
    "pak": "pak", "pakuote": "pak", "pakuot": "pak", "pack": "pak", "pkg": "pak",
    "kompl": "kompl", "komplektas": "kompl", "set": "kompl",
    "rul": "rul", "ritinys": "rul", "roll": "rul",
    "l": "l", "ltr": "l", "litras": "l",
    "m2": "m2", "m^2": "m2", "kv m": "m2", "kvm": "m2",
    "m3": "m3", "m^3": "m3",
    "m": "m", "metras": "m",
    "kg": "kg", "g": "g", "t": "t",
    "pora": "pora", "pair": "pora",
    "val": "val", "h": "val",
}


def norm_matas(v) -> str:
    """'DĖŽ.' -> 'dez', 'Vnt' -> 'vnt', '' -> ''. Nezinomi matai lieka kaip yra."""
    s = be_diakritikos(str(v or "").strip().lower()).strip(" .")
    s = s.replace("²", "2").replace("³", "3")
    return _MATU_SINONIMAI.get(s, s)

BALSES = "aeiouy"


def be_diakritikos(t):
    return "".join(c for c in unicodedata.normalize("NFD", t)
                   if unicodedata.category(c) != "Mn")


def istraukti_skaicius(tekstas):
    t = str(tekstas).lower()
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"(\d)\.(\d+)\.(\d)", r"\1 \2 \3", t)
    sk = []
    for s in re.findall(r"\d+(?:\.\d+)?", t):
        v = float(s)
        if v == int(v):
            v = int(v)
        sk.append(v)
    return sorted(set(sk))


def tekstas_be_skaiciu(tekstas):
    t = str(tekstas).lower()
    t = re.sub(r"\d+[\d.,x×*/-]*", " ", t)
    t = re.sub(r"\b(mm|cm|m|kg|g|l|ml|vnt|pak|mk|t)\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def saknis(zodis):
    z = be_diakritikos(zodis.lower())
    buvo = None
    while buvo != z:
        buvo = z
        if len(z) > 3 and z[-1] == "s":
            z = z[:-1]
        while len(z) > 3 and z[-1] in BALSES:
            z = z[:-1]
    return z


def saknys(tekstas):
    return {saknis(z) for z in tekstas.split() if len(z) >= 3}


# ── SKAICIU SLUOKSNIS v2 ────────────────────────────────────────────────────
# Katalogo analize (2026-08-15) parode: 331 tukst. korteliu poru turi IDENTISKA
# vektoriu (tekstas be skaiciu sutampa) — seimos viduje ("OSB 12mm" vs "18mm")
# rikiuoja TIK skaiciai. v2 supranta tris dalykus, kuriu v1 nesuprato:
#   1) matmenu tvarka nesvarbi: 1250x2500x12 == 2500x1250x12 == "12mm 2500x1250"
#      (lyginama VISU skaiciu aibe, ne seka);
#   2) zymejimas: 4*100 == 4x100 == 4×100, kablelis == taskas;
#   3) vienetai: 50m ir 50mm NE tas pats — tas pats skaicius su skirtingais
#      matais yra KONFLIKTAS, ne sutapimas.
# Ir dvi naujos taisykles rikiavimui:
#   - abu turi skaicius, bet nesutampa NE VIENAS -> stiprus minusas (veto);
#   - eilute su parametrais, kortele visai be ju ("Dazai") -> lengvas minusas,
#     kad bendrines korteles-atraktoriai nebeuzstotu tiksliu varianteliu.

_VIENETAI = r"(?:mm2|mm²|m2|m²|m3|m³|mm|cm|ml|mah|ah|kg|kw|mpa|bar|vnt|col|mm\.|m|g|l|w|v|a|\"|'')"


def skaiciu_faktai(tekstas):
    """(visi_skaiciai_multiset, {skaicius: vienetas}) is pavadinimo."""
    t = str(tekstas).lower()
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)          # kablelis -> taskas
    t = t.replace("×", "x").replace("*", "x")       # zymejimo suvienodinimas
    t = re.sub(r"(\d)\.(\d+)\.(\d)", r"\1 \2 \3", t)
    vienetai = {}
    for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*({_VIENETAI})(?![a-z0-9])", t):
        v = float(m.group(1))
        vnt = m.group(2).rstrip(".")
        vnt = {"mm²": "mm2", "m²": "m2", "m³": "m3",
               "''": "col", '"': "col"}.get(vnt, vnt)
        vienetai[int(v) if v == int(v) else v] = vnt
    visi = []
    for s in re.findall(r"\d+(?:\.\d+)?", t):
        v = float(s)
        visi.append(int(v) if v == int(v) else v)
    return tuple(sorted(visi)), vienetai


def skaiciu_zyme2(eil_faktai, kort_faktai):
    """v2 palyginimas. Grazina (priedas_prie_balo, zyme)."""
    eil_visi, eil_vnt = eil_faktai
    kort_visi, kort_vnt = kort_faktai
    if not eil_visi and not kort_visi:
        return 0.0, "? nera sk."
    if not eil_visi:
        return 0.0, "? nera sk."
    if not kort_visi:
        # Eilute turi parametrus, kortele — ne: arba bendrine kortele-atraktorius,
        # arba nukirptas vardas. Lengvas minusas — po kortele SU sutampanciais
        # parametrais, bet vis dar virs korteliu su KONFLIKTUOJANCIAIS.
        return -0.10, "? be sk."

    # Vienetu konfliktas: tas pats skaicius su AISKIAI nesuderinamais matais
    # (50m vs 50mm). TIK sios poros — kitur zmones patys raso pramaisiui
    # (kabeliu 1.5mm == 1.5mm2, tad mm vs mm2 NE konfliktas).
    NESUDERINAMI = {frozenset(p) for p in (("m", "mm"), ("m", "cm"), ("l", "ml"),
                                            ("kg", "g"), ("m2", "mm2"))}
    for v, vnt in eil_vnt.items():
        kitas = kort_vnt.get(v)
        if kitas and kitas != vnt and frozenset((vnt, kitas)) in NESUDERINAMI:
            return -BAUDA * 3, f"- {v}{vnt} vs {v}{kitas}"

    if eil_visi == kort_visi:
        return SV_SK, "+ visi sk."
    # MULTIAIBES, ne aibes: 600x600x600 vs 1200x600x600 turi skirtis,
    # nors unikalios reiksmes butu poaibis
    ce, ck = Counter(eil_visi), Counter(kort_visi)
    if ck <= ce:
        return SV_SK * (sum(ck.values()) / sum(ce.values())), f"+ {sum(ck.values())}/{sum(ce.values())} sk."
    if ce <= ck:
        return SV_SK * (sum(ce.values()) / sum(ck.values())), f"+ {sum(ce.values())}/{sum(ck.values())} sk."
    se, sk = set(eil_visi), set(kort_visi)
    if su_daugikliu_telpa(sk, se) or su_daugikliu_telpa(se, sk):
        return SV_SK_VNT * 0.8, "~ kiti vnt."
    if se & sk:
        return -BAUDA, "- konfliktas"
    # Nesutampa NE VIENAS skaicius — beveik garantuotai kitas variantas
    return -BAUDA * 3, "- kiti matmenys"


def su_daugikliu_telpa(a, b):
    if not a:
        return False
    for k in (10, 100, 1000):
        if {round(v * k, 3) for v in a} <= b:
            if len(a) >= 2:
                return True
            v = next(iter(a))
            if v != int(v):
                return True
    return False


def skaiciu_zyme(eil_sk, kort_sk):
    """Priedas proporcingas padengimui: 1 is 3 skaiciu = trecdalis priedo."""
    if not kort_sk or not eil_sk:
        return 0.0, "? nera sk."
    se, sk = set(eil_sk), set(kort_sk)
    if sk <= se:
        dalis = len(sk) / len(se)
        return SV_SK * dalis, f"+ {len(sk)}/{len(se)} sk."
    if se <= sk:
        dalis = len(se) / len(sk)
        return SV_SK * dalis, f"+ {len(se)}/{len(sk)} sk."
    if su_daugikliu_telpa(sk, se) or su_daugikliu_telpa(se, sk):
        return SV_SK_VNT * 0.8, "~ kiti vnt."
    return -BAUDA, "- konfliktas"


class Katalogas:
    """Uzkrauna .npz (vektoriai + kodai + pavadinimai) ir vykdo paieska."""

    def __init__(self, npz_kelias, openai_client):
        d = np.load(npz_kelias, allow_pickle=True)
        vekt = d["vektoriai"]
        self.KODAI = d["kodai"]
        self.PAVADINIMAI = d["pavadinimai"]
        self.TEKSTAI = d["tekstai"]
        self.SKAICIAI = [json.loads(s) for s in d["skaiciai"]]
        self.N = len(self.KODAI)
        # Matai (vnt/dez/pak...) — rodomi rekomendacijose ir duoda balo prieda,
        # kai sutampa su saskaitos eilutes vienetu. Senas npz be matu — tusti.
        self.MATAI = [str(x) for x in d["matai"]] if "matai" in d else [""] * self.N
        self.MATAI_NORM = [norm_matas(x) for x in self.MATAI]
        # TALPOS REZERVAS: lentele sukuriama su laisvomis vietomis gale, kad
        # Pragmos papildymai rasytusi I VIETA be pilnos ~800 MB kopijos
        # (kopija liktu tik retam atvejui, kai rezervas issenka).
        # Paieska VISUR naudoja tik gyva dali: self.VEKTORIAI[:self.N].
        self.TALPA = self.N + self._rezervas(self.N)
        self.VEKTORIAI = np.empty((self.TALPA, vekt.shape[1] if vekt.ndim == 2 else 0),
                                  dtype=vekt.dtype)
        self.VEKTORIAI[:self.N] = vekt
        del vekt, d
        self.client = openai_client
        # v2 faktai skaiciuojami is pavadinimu uzkraunant (nepriklauso nuo npz formato)
        self.FAKTAI = [skaiciu_faktai(str(p)) for p in self.PAVADINIMAI]

        self.SAKNU_SETAI = [saknys(str(t)) for t in self.TEKSTAI]
        self.DF = Counter()
        self.INDEKSAS = defaultdict(set)
        for i, ss in enumerate(self.SAKNU_SETAI):
            for s in ss:
                self.DF[s] += 1
                self.INDEKSAS[s].add(i)

    @staticmethod
    def _rezervas(n: int) -> int:
        return max(4096, n // 16)

    def prideti(self, poros, npz_kelias=None):
        """Prideda/atnaujina korteles GYVAI (Pragmos importas praneša, kokias
        sukūrė ar rado). `poros` = [(kodas, pavadinimas), ...].

        Vektorius skaičiuojamas tik NAUJIEMS pavadinimams — jei kodas jau yra ir
        vardas nepasikeitė, nedaroma nieko. Grąžina {pridėta, atnaujinta, praleista}.
        Nurodžius npz_kelias, katalogas iškart įrašomas į diską (kad išliktų po
        perkrovimo)."""
        vieta = {str(k): i for i, k in enumerate(self.KODAI)}
        nauji, keiciami = [], []          # (kodas, pav) / (indeksas, kodas, pav)
        for kodas, pav in poros:
            kodas, pav = str(kodas or "").strip(), str(pav or "").strip()
            if not kodas or not pav:
                continue
            i = vieta.get(kodas)
            if i is None:
                nauji.append((kodas, pav))
            elif str(self.PAVADINIMAI[i]) != pav:
                keiciami.append((i, kodas, pav))

        darbas = [p for _, p in nauji] + [p for _, _, p in keiciami]
        if not darbas:
            return {"prideta": 0, "atnaujinta": 0, "praleista": len(poros)}

        tekstai = [tekstas_be_skaiciu(p) or "preke" for p in darbas]
        dalys = [self._embeddingai(tekstai[i:i + 500]) for i in range(0, len(tekstai), 500)]
        v = np.vstack(dalys)

        for j, (i, _kodas, pav) in enumerate(keiciami):
            v_eil = v[len(nauji) + j]
            self.VEKTORIAI[i] = v_eil
            self.PAVADINIMAI[i] = pav
            self.TEKSTAI[i] = tekstai[len(nauji) + j]
            self.SKAICIAI[i] = istraukti_skaicius(pav)
            self.FAKTAI[i] = skaiciu_faktai(pav)
            senos = self.SAKNU_SETAI[i]
            for s in senos:
                self.DF[s] -= 1
                self.INDEKSAS[s].discard(i)
            naujos = saknys(self.TEKSTAI[i])
            self.SAKNU_SETAI[i] = naujos
            for s in naujos:
                self.DF[s] += 1
                self.INDEKSAS[s].add(i)

        if nauji:
            # naujos korteles is Pragmos ateina be mato — tuscias, kol atsiras eksporte
            self.MATAI.extend([""] * len(nauji))
            self.MATAI_NORM.extend([""] * len(nauji))
            reikia = len(nauji)
            if self.N + reikia > self.TALPA:
                # Rezervas isseko (retas ivykis) — nauja lentele su nauju rezervu.
                # Tik cia ivyksta pilna kopija; kasdieniai papildymai rasomi i vieta.
                nauja_talpa = self.N + reikia + self._rezervas(self.N + reikia)
                nauja = np.empty((nauja_talpa, self.VEKTORIAI.shape[1]), dtype=self.VEKTORIAI.dtype)
                nauja[:self.N] = self.VEKTORIAI[:self.N]
                self.VEKTORIAI = nauja
                self.TALPA = nauja_talpa
            self.VEKTORIAI[self.N:self.N + reikia] = v[:reikia]
            self.KODAI = np.append(self.KODAI, np.array([k for k, _ in nauji], dtype=object))
            self.PAVADINIMAI = np.append(self.PAVADINIMAI, np.array([p for _, p in nauji], dtype=object))
            self.TEKSTAI = np.append(self.TEKSTAI, np.array(tekstai[:len(nauji)], dtype=object))
            for j, (_kodas, pav) in enumerate(nauji):
                i = self.N + j
                self.SKAICIAI.append(istraukti_skaicius(pav))
                self.FAKTAI.append(skaiciu_faktai(pav))
                ss = saknys(tekstai[j])
                self.SAKNU_SETAI.append(ss)
                for s in ss:
                    self.DF[s] += 1
                    self.INDEKSAS[s].add(i)
            self.N += len(nauji)

        if npz_kelias:
            self.issaugoti_npz(npz_kelias)

        return {"prideta": len(nauji), "atnaujinta": len(keiciami),
                "praleista": len(poros) - len(nauji) - len(keiciami)}

    def issaugoti_npz(self, npz_kelias):
        laik = npz_kelias + ".tmp.npz"
        np.savez_compressed(
            laik,
            vektoriai=self.VEKTORIAI[:self.N],
            kodai=np.array(list(self.KODAI), dtype=object),
            pavadinimai=np.array(list(self.PAVADINIMAI), dtype=object),
            tekstai=np.array(list(self.TEKSTAI), dtype=object),
            skaiciai=np.array([json.dumps(s) for s in self.SKAICIAI]),
            matai=np.array(self.MATAI[:self.N], dtype=object),
        )
        os.replace(laik, npz_kelias)

    def _idf(self, s):
        return math.log(1 + self.N / max(1, self.DF.get(s, 0)))

    def _pagrindine_saknis(self, uzkl_saknys, pirmas_zodis_saknis):
        if pirmas_zodis_saknis in uzkl_saknys:
            return pirmas_zodis_saknis
        if not uzkl_saknys:
            return None
        return max(uzkl_saknys, key=self._idf)

    def _zodziu_balas(self, uzkl_saknys, pagr, i):
        if not uzkl_saknys:
            return 0.0
        bendros = uzkl_saknys & self.SAKNU_SETAI[i]
        if not bendros:
            return 0.0
        balas = sum(self._idf(s) for s in bendros) / sum(self._idf(s) for s in uzkl_saknys)
        if pagr and pagr not in self.SAKNU_SETAI[i]:
            balas *= 0.5
        return balas

    def _ivertinti(self, eilute, v, top_n, vienetas=None):
        """Balu skaiciavimas vienai eilutei, kai embeddingas jau gautas.
        `vienetas` — saskaitos eilutes matas (vnt/dez/pak...): sutampantis
        korteles matas gauna prieda (dezemis pirkta -> dezemis ir kortele)."""
        vnt_norm = norm_matas(vienetas)
        tekstas = tekstas_be_skaiciu(eilute) or "preke"
        eil_faktai = skaiciu_faktai(eilute)
        uzkl_s = saknys(tekstas)
        zodziai = tekstas.split()
        pagr = self._pagrindine_saknis(uzkl_s, saknis(zodziai[0]) if zodziai else None)

        panasumai = self.VEKTORIAI[:self.N] @ v

        kand = set(np.argsort(-panasumai)[:150].tolist())
        riba = max(3, int(self.N * 0.02))
        for s in uzkl_s:
            if 0 < self.DF.get(s, 0) <= riba:
                kand |= self.INDEKSAS[s]

        eil_norm = re.sub(r"\s+", " ", be_diakritikos(str(eilute).lower())).strip(" .,")
        rez = []
        for i in kand:
            vekt = float(panasumai[i])
            zod = self._zodziu_balas(uzkl_s, pagr, i)
            pr, zyme = skaiciu_zyme2(eil_faktai, self.FAKTAI[i])
            balas = SV_VEKT * vekt + SV_ZOD * zod + pr
            if vnt_norm and self.MATAI_NORM[i] == vnt_norm:
                balas += SV_MATO
            # Nera NE VIENO bendro zodzio ("maisai" siuloma prie "betono"):
            # vektorius mato gimininga sriti, bet pavadinimai nesusicaukia — minusas.
            if uzkl_s and zod == 0.0:
                balas -= BAUDA
            # Pavadinimas RAIDE I RAIDE sutampa su korteles — tai ta pati preke,
            # jokia gimininga kortele negali jos aplenkti.
            kort_norm = re.sub(r"\s+", " ", be_diakritikos(str(self.PAVADINIMAI[i]).lower())).strip(" .,")
            if eil_norm and eil_norm == kort_norm:
                balas += SV_SK
                zyme = "= tikslus vardas"
            rez.append((balas, vekt, zod, zyme, str(self.KODAI[i]), str(self.PAVADINIMAI[i]), self.MATAI[i]))

        # Stabilus rusiavimas (prie lygiu balu — pagal koda), kad tvarka nesikeistu
        rez.sort(key=lambda x: (-x[0], x[4]))

        # DUBLIKATU SULIPDYMAS: kai ta pati preke kataloge guli keliais kodais
        # (vardas sutampa raide i raide), siulomas VISADA TAS PATS VIENAS kodas —
        # DIDZIAUSIAS numeris (naujausia kortele; užsakovo taisykle 2026-08-15).
        # Kiti tos grupes kodai neberodomi niekada — kad saskaitose nesimaisytu
        # cia vienas, cia kitas. Atmintyje isimintas užsakovo pasirinkimas viresnis
        # (atmintis suveikia dar pries paieska).
        def _kodo_eile(k):
            m = re.fullmatch(r"([A-Za-z]+)(\d+)", k)
            if m and m.group(1).upper() == "PR":
                return (1, int(m.group(2)), k)      # PR kodai pirmenybe, didziausias numeris
            return (0, 0, k)
        out = []
        matyti_vardai = {}
        virsus = rez[0][0] if rez else 0
        for g, vekt, zod, zyme, kodas, pav, matas in rez:
            raktas = re.sub(r"\s+", " ", be_diakritikos(pav.lower())).strip()
            if raktas in matyti_vardai:
                esamas = matyti_vardai[raktas]
                kiti = esamas.setdefault("kiti_kodai", [])
                # balai lygus (vardas tas pats) — atstovu tampa naujausias kodas
                if abs(esamas["balas"] - round(float(g), 3)) < 0.002 and \
                        _kodo_eile(kodas) > _kodo_eile(esamas["kodas"]):
                    kiti.append(esamas["kodas"])
                    esamas["kodas"] = kodas
                else:
                    kiti.append(kodas)
                continue
            irasas = {
                "kodas": kodas,
                "pavadinimas": pav,
                "vienetas": matas,
                "balas": round(float(g), 3),
                "vekt": round(float(vekt), 2),
                "zod": round(float(zod), 2),
                "zyme": zyme,
                "zemiau_luzio": bool(virsus > 0 and g < virsus * 0.5),
            }
            matyti_vardai[raktas] = irasas
            out.append(irasas)
            if len(out) >= top_n:
                break
        return out

    def _embeddingai(self, tekstai):
        if (os.environ.get("EMBEDDING_TIEKEJAS") or "openai").lower() == "gemini":
            v = _gemini_embeddingai(tekstai)
        else:
            r = self.client.embeddings.create(model=MODELIS, input=tekstai)
            v = np.array([d.embedding for d in r.data], dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        return v

    # Viesi variantai (serveriui, kai vektoriai skaiciuojami atskirai nuo vertinimo)
    def embeddingai(self, tekstai):
        return self._embeddingai(tekstai)

    def ivertinti(self, eilute, v, top_n=6, vienetas=None):
        return self._ivertinti(eilute, v, top_n, vienetas)

    def terminai_pagal_vektoriu(self, v, kandidatu=150, daugiausiai=40):
        """Preku TIPU terminai (pirmi 2 pavadinimo zodziai) is vektoriskai artimiausiu
        korteliu — AI sinonimu parinkimui. Veikia ir 60k katalogui (imami tik kandidatai)."""
        panasumai = self.VEKTORIAI[:self.N] @ v
        terminai, matyti = [], set()
        for i in np.argsort(-panasumai)[:kandidatu]:
            zodziai = [z.rstrip(",.") for z in str(self.PAVADINIMAI[i]).split()]
            t = " ".join(zodziai[:2])
            k = t.lower()
            if k and k not in matyti:
                matyti.add(k)
                terminai.append(t)
            if len(terminai) >= daugiausiai:
                break
        return terminai

    def ieskoti(self, eilute, top_n=12, vienetas=None):
        """Viena uzklausa (paieskos langelis) — 1 embedding kvietimas."""
        tekstas = tekstas_be_skaiciu(eilute) or "preke"
        v = self._embeddingai([tekstas])[0]
        return self._ivertinti(eilute, v, top_n, vienetas)

    def ieskoti_daug(self, eilutes, top_n=6, vienetai=None):
        """Visos saskaitos eilutes VIENU embedding kvietimu (pigiau ir greiciau)."""
        if not eilutes:
            return []
        tekstai = [tekstas_be_skaiciu(e) or "preke" for e in eilutes]
        vv = self._embeddingai(tekstai)
        return [self._ivertinti(e, vv[i], top_n, (vienetai or [None] * len(eilutes))[i])
                for i, e in enumerate(eilutes)]

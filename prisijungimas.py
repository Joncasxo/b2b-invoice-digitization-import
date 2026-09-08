"""
Prieigos kontrole, kai programa gyvena internete (nuomotame serveryje).

Du atskiri raktai (.env):
  PRISIJUNGIMO_SLAPTAZODIS — bendras darbuotoju slaptazodis. TUSCIAS = prisijungimo
    NEREIKIA (lokalus rezimas, viskas kaip iki siol).
  AGENTO_RAKTAS — raktas Pragmos serverio agentui (X-Agento-Raktas antraste).
    TUSCIAS = agento API isjungtas.

Sesija — pasirasytas cookie (vardas + laikas + HMAC), jokiu irasu diske.
Galiojimas SLENKANTIS: kol zmogus dirba, sesija atsinaujina; pabuvus be
veiklos ilgiau nei SESIJOS_MIN (.env, numatyta 30 min) — prisijungti is naujo.
"""

import hashlib
import hmac
import os
import time


def _slaptazodis() -> str:
    return (os.environ.get("PRISIJUNGIMO_SLAPTAZODIS") or "").strip()


def ijungtas() -> bool:
    """Ar prisijungimas privalomas (slaptazodis nustatytas .env)."""
    return bool(_slaptazodis())


def _raktas() -> bytes:
    return hashlib.sha256(("uzp-sesija|" + _slaptazodis()).encode("utf-8")).digest()


def _parasas(dalis: str) -> str:
    return hmac.new(_raktas(), dalis.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def galiojimo_s() -> int:
    try:
        minutes = int(os.environ.get("SESIJOS_MIN") or "30")
    except ValueError:
        minutes = 30
    return max(1, minutes) * 60


def sesijos_cookie(vardas: str) -> str:
    v = vardas.encode("utf-8").hex()
    ts = str(int(time.time()))
    return f"{v}.{ts}.{_parasas(v + '|' + ts)}"


def vardas_is_cookie(reiksme: str) -> str:
    """Grazina varda, jei cookie parasas geras ir sesija nepasenusi; kitaip ''.
    Senas 2-daliu formatas (be laiko) nebegalioja — visi prisijungia is naujo."""
    dalys = (reiksme or "").split(".")
    if len(dalys) != 3:
        return ""
    v, ts, p = dalys
    if not v or not hmac.compare_digest(p, _parasas(v + "|" + ts)):
        return ""
    try:
        if time.time() - int(ts) > galiojimo_s():
            return ""
        return bytes.fromhex(v).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def slaptazodis_geras(bandymas: str) -> bool:
    # lyginami BAITAI: compare_digest su ne-ASCII simboliais (pvz. lietuviska
    # raide slaptazodyje) meta TypeError -> 500 vietoj 401 (09-08 atvejis)
    return ijungtas() and hmac.compare_digest((bandymas or "").encode("utf-8"), _slaptazodis().encode("utf-8"))


def agento_raktas_geras(bandymas: str) -> bool:
    tikras = (os.environ.get("AGENTO_RAKTAS") or "").strip()
    return bool(tikras) and hmac.compare_digest((bandymas or "").encode("utf-8"), tikras.encode("utf-8"))


def saugus_cookie() -> bool:
    """Secure flag'as tik kai programa pasiekiama per https (VIESAS_URL)."""
    return (os.environ.get("VIESAS_URL") or "").strip().lower().startswith("https://")

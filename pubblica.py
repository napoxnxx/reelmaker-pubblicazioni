"""Creato da ReelMaker. Gira su GitHub ogni 15 minuti (anche a PC spento):
pubblica su Instagram i reel programmati la cui ora è arrivata.

- programma/<id>.json : un file per reel (data, didascalia, stato) scritto da ReelMaker
- i video stanno nel ramo "media", pubblicato da GitHub Pages: Instagram li scarica da lì
- stato/token.json   : quando è stato rinnovato l'ultima volta il token di Instagram
"""
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

IG_API = os.environ.get("IG_API", "https://graph.instagram.com")
GH_API = os.environ.get("GITHUB_API", "https://api.github.com")
ADESSO = dt.datetime.now(dt.timezone.utc)
MAX_TENTATIVI = 8


class Errore(Exception):
    pass


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def _check(r):
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise Errore(err.get("error_user_msg") or err.get("message") or r.text[:300])
    return data


def leggi(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scrivi(path, dati):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(dati, indent=1, ensure_ascii=False), encoding="utf-8")


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def salva_su_github(file, messaggio):
    """Commit e push (con un nuovo tentativo se ReelMaker ha scritto nel frattempo)."""
    git("config", "user.name", "ReelMaker")
    git("config", "user.email", "reelmaker@users.noreply.github.com")
    git("add", *file)
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        return True
    git("commit", "-m", messaggio)
    for _ in range(4):
        if git("push", "origin", "HEAD:main", check=False).returncode == 0:
            return True
        git("pull", "--rebase", "origin", "main", check=False)
    log("Non riesco a salvare lo stato su GitHub")
    return False


# ------------------------------------------------------------ token Instagram
def aggiorna_segreto(nome, valore):
    from nacl import encoding, public
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("REPO")
    if not pat or not repo:
        raise Errore("manca GH_PAT")
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    chiave = _check(requests.get(f"{GH_API}/repos/{repo}/actions/secrets/public-key", headers=h,
                                 timeout=30))
    box = public.SealedBox(public.PublicKey(chiave["key"].encode(), encoding.Base64Encoder()))
    cifrato = base64.b64encode(box.encrypt(valore.encode())).decode()
    r = requests.put(f"{GH_API}/repos/{repo}/actions/secrets/{nome}", headers=h, timeout=30,
                     json={"encrypted_value": cifrato, "key_id": chiave["key_id"]})
    if r.status_code not in (201, 204):
        raise Errore(f"segreto non aggiornato ({r.status_code})")


def token_instagram(cambiati):
    """I token di Instagram durano 60 giorni: ogni 20 giorni lo rinnovo da solo."""
    token = os.environ["IG_TOKEN"]
    f = Path("stato/token.json")
    stato = leggi(f) if f.exists() else {}
    ultimo = stato.get("rinnovato_il")
    if ultimo and (ADESSO.date() - dt.date.fromisoformat(ultimo)).days < 20:
        return token
    try:
        nuovo = _check(requests.get(f"{IG_API}/refresh_access_token", timeout=30, params={
            "grant_type": "ig_refresh_token", "access_token": token}))["access_token"]
        aggiorna_segreto("IG_TOKEN", nuovo)
        scrivi(f, {"rinnovato_il": ADESSO.date().isoformat()})
        cambiati.append(str(f))
        log("Token di Instagram rinnovato.")
        return nuovo
    except Exception as e:  # noqa: BLE001
        log(f"Rinnovo del token non riuscito ({e}): uso quello di prima.")
        return token


# ------------------------------------------------------------ pubblicazione
def gia_pubblicato(r, token, uid, versione):
    """Dopo un'interruzione: il reel è già su Instagram? (stessa didascalia, ultime 48 ore)"""
    try:
        dati = _check(requests.get(f"{IG_API}/{versione}/{uid}/media", timeout=30, params={
            "fields": "caption,permalink,timestamp", "limit": 15, "access_token": token}))
    except Exception:  # noqa: BLE001
        return None
    inizio = (r.get("didascalia") or "").strip()[:80]
    for m in dati.get("data", []):
        if inizio and (m.get("caption") or "").strip()[:80] == inizio:
            return m.get("permalink") or "pubblicato"
    return None


def _contenitore(r, token, uid, url, versione):
    cont = _check(requests.post(f"{IG_API}/{versione}/{uid}/media", timeout=120, data={
        "media_type": "REELS", "video_url": url, "caption": r.get("didascalia", ""),
        "share_to_feed": "true" if r.get("condividi_nel_feed", True) else "false",
        "access_token": token}))
    cid = cont["id"]
    for _ in range(180):
        st = _check(requests.get(f"{IG_API}/{versione}/{cid}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}))
        if st.get("status_code") == "FINISHED":
            return cid
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise Errore(f"Instagram ha rifiutato il video: {st.get('status')}")
        time.sleep(5)
    raise Errore("Instagram non ha finito di elaborare il video")


def pubblica(r, token, uid, indirizzi):
    """indirizzi: GitHub Pages e raw. Uso il primo che risponde e che Instagram accetta."""
    versione = r.get("versione_api") or "v23.0"
    errori, cid = [], None
    for base in indirizzi:
        if not base:
            continue
        url = base.rstrip("/") + "/" + r["file"]
        try:
            v = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=60)
            if v.status_code not in (200, 206):
                raise Errore(f"il video non è online ({v.status_code})")
            cid = _contenitore(r, token, uid, url, versione)
            break
        except Exception as e:  # noqa: BLE001
            log(f"{url}: {e}")
            errori.append(str(e))
            if "token" in str(e).lower():
                break
    if not cid:
        raise Errore(" | ".join(errori) or "nessun indirizzo del video")
    pub = _check(requests.post(f"{IG_API}/{versione}/{uid}/media_publish", timeout=60, data={
        "creation_id": cid, "access_token": token}))
    try:
        return _check(requests.get(f"{IG_API}/{versione}/{pub['id']}", timeout=30, params={
            "fields": "permalink", "access_token": token})).get("permalink", "")
    except Exception:  # noqa: BLE001
        return ""


def main():
    cambiati = []
    cfg = leggi("impostazioni.json") if Path("impostazioni.json").exists() else {}
    indirizzi = [cfg.get("pages_url", ""), cfg.get("raw_url", "")]
    uid = os.environ.get("IG_USER_ID", "")
    dovuti = []
    for f in sorted(Path("programma").glob("*.json")):
        r = leggi(f)
        quando = dt.datetime.fromisoformat(r["quando"])
        if quando.tzinfo is None:
            quando = quando.replace(tzinfo=dt.timezone.utc)
        if r.get("stato") in ("programmato", "in_pubblicazione") and quando <= ADESSO and \
                r.get("tentativi", 0) < MAX_TENTATIVI:
            dovuti.append((f, r))
    token = token_instagram(cambiati) if os.environ.get("IG_TOKEN") else ""
    for f, r in dovuti:
        if not token or not uid:
            log("Mancano i dati di Instagram (IG_TOKEN / IG_USER_ID)")
            break
        versione = r.get("versione_api") or "v23.0"
        if r.get("stato") == "in_pubblicazione":
            # un giro precedente si è interrotto a metà: controllo prima di ripubblicare
            link = gia_pubblicato(r, token, uid, versione)
            if link:
                r.update(stato="pubblicato", link=link, pubblicato_il=ADESSO.isoformat())
                scrivi(f, r)
                cambiati.append(str(f))
                continue
        # segno subito "in pubblicazione": se due giri partissero insieme non lo pubblico due volte
        r["stato"] = "in_pubblicazione"
        scrivi(f, r)
        if not salva_su_github([str(f)], f"Pubblico {f.stem}"):
            continue
        try:
            link = pubblica(r, token, uid, indirizzi)
            r.update(stato="pubblicato", link=link, pubblicato_il=ADESSO.isoformat(), errore="")
            log(f"Pubblicato {f.stem}: {link}")
        except Exception as e:  # noqa: BLE001
            r["tentativi"] = r.get("tentativi", 0) + 1
            r["stato"] = "programmato"
            r["errore"] = str(e)[:300]
            log(f"Non pubblicato {f.stem}: {e}")
        scrivi(f, r)
        cambiati.append(str(f))
    # una volta al giorno lascio un segno: GitHub non spegne i controlli dei progetti attivi
    vivo = Path("stato/ultimo_controllo.json")
    if not vivo.exists() or leggi(vivo).get("giorno") != ADESSO.date().isoformat():
        scrivi(vivo, {"giorno": ADESSO.date().isoformat()})
        cambiati.append(str(vivo))
        try:   # e mi assicuro che il controllo automatico resti acceso
            requests.put(f"{GH_API}/repos/{os.environ['REPO']}/actions/workflows/pubblica.yml/enable",
                         headers={"Authorization": f"Bearer {os.environ['GH_PAT']}",
                                  "Accept": "application/vnd.github+json"}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    if cambiati:
        salva_su_github(sorted(set(cambiati)), "Aggiorno lo stato delle pubblicazioni")
    return 0


if __name__ == "__main__":
    sys.exit(main())

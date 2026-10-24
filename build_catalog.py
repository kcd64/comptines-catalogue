"""Construit le catalogue de musiques pour enfants à partir de chaînes YouTube validées.

  python build_catalog.py decouvrir   -> data/chaines_trouvees.csv (à valider dans Excel)
  python build_catalog.py construire  -> data/catalogue.json
  python build_catalog.py status      -> quota utilisé et résumé du catalogue
"""
import argparse
import csv
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
import yaml

API = "https://www.googleapis.com/youtube/v3"
DATA = Path("data")


class QuotaEpuise(Exception):
    pass


def charger_env(chemin=".env"):
    p = Path(chemin)
    if p.exists():
        for ligne in p.read_text(encoding="utf-8").splitlines():
            if "=" in ligne and not ligne.strip().startswith("#"):
                k, v = ligne.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class YouTube:
    """Appels à l'API YouTube avec cache disque et compteur de quota."""

    def __init__(self, cle, jours_cache):
        self.cle, self.jours, self.quota = cle, jours_cache, 0
        self.cache = DATA / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def appel(self, ressource, cout, **params):
        empreinte = hashlib.sha1(json.dumps([ressource, params], sort_keys=True).encode()).hexdigest()
        f = self.cache / f"{empreinte}.json"
        if f.exists() and time.time() - f.stat().st_mtime < self.jours * 86400:
            return json.loads(f.read_text(encoding="utf-8"))
        r = httpx.get(f"{API}/{ressource}", params={**params, "key": self.cle}, timeout=30)
        if r.status_code == 403 and "quota" in r.text.lower():
            raise QuotaEpuise("Quota YouTube épuisé pour aujourd'hui. Relance demain : le cache évite de tout refaire.")
        r.raise_for_status()
        self.quota += cout
        f.write_text(r.text, encoding="utf-8")
        return r.json()


# ---------- fonctions pures (testées) ----------
def duree_secondes(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    j, h, mi, s = (int(x or 0) for x in m.groups())
    return ((j * 24 + h) * 60 + mi) * 60 + s


def playlist_envois(chaine_id):
    """Playlist « mises en ligne » d'une chaîne : même identifiant avec UC remplacé par UU."""
    return "UU" + chaine_id[2:] if chaine_id.startswith("UC") else chaine_id


def evaluer(v, cfg, excl=None):
    """Renvoie None si la vidéo est gardée, sinon la raison du rejet."""
    f, st, cd = cfg["filtres"], v.get("status", {}), v.get("contentDetails", {})
    titre = sans_accents(v.get("snippet", {}).get("title", ""))
    excl = excl or {"mots": [], "chaines": set(), "videos": set()}
    if v.get("id") in excl["videos"] or v.get("snippet", {}).get("channelId") in excl["chaines"]:
        return "exclue à la main"
    if any(m in titre for m in excl["mots"]):
        return "exclue à la main (mot)"
    if st.get("privacyStatus") != "public":
        return "non publique"
    if not st.get("embeddable", False):
        return "intégration bloquée"
    if v.get("snippet", {}).get("liveBroadcastContent", "none") != "none":
        return "direct"
    rr = cd.get("regionRestriction", {})
    if cfg["region"] in rr.get("blocked", []) or ("allowed" in rr and cfg["region"] not in rr["allowed"]):
        return "bloquée dans la région"
    d = duree_secondes(cd.get("duration"))
    if not f["duree_min_s"] <= d <= f["duree_max_s"]:
        return "durée hors limites"
    if f["exiger_made_for_kids"] and not st.get("madeForKids"):
        return "non déclarée pour enfants"
    if any(m in titre for m in _normaliser_mots(f["mots_exclus"])):
        return "mot exclu dans le titre"
    return None


def sans_accents(texte):
    """Minuscules sans accents : « Bébé » et « bebe » deviennent identiques."""
    t = unicodedata.normalize("NFD", str(texte or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _normaliser_mots(mots):
    return list(dict.fromkeys(sans_accents(m) for m in mots))  # sans doublons (bébé = bebe)


def cle_titre(titre):
    """Titre simplifié pour repérer les doublons (ignore parenthèses, ponctuation, « paroles », « hd »…)."""
    t = re.sub(r"[\(\[].*?[\)\]]", " ", sans_accents(titre))
    t = re.sub(r"\b(paroles|lyrics|hd|officiel|official|clip|video|version|karaoke)\b", " ", t)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def charger_exclusions(chemin=None):
    """data/exclusions.txt : lignes « mot: xxx », « chaine: UC… » ou « video: id ». # = commentaire."""
    chemin = Path(chemin) if chemin else DATA / "exclusions.txt"
    ex = {"mots": [], "chaines": set(), "videos": set()}
    if not chemin.exists():
        return ex
    for ligne in chemin.read_text(encoding="utf-8-sig").splitlines():
        ligne = ligne.split("#")[0].strip()
        if ":" not in ligne:
            continue
        type_, valeur = (x.strip() for x in ligne.split(":", 1))
        if type_ == "mot" and valeur:
            ex["mots"].append(sans_accents(valeur))
        elif type_ == "chaine" and valeur:
            ex["chaines"].add(valeur)
        elif type_ == "video" and valeur:
            ex["videos"].add(valeur)
    return ex


def est_doublon(c, vus):
    """vus : cle_titre -> liste de (chaine_id, durée). Doublon = même chaîne, ou durée à ±3 s."""
    k = cle_titre(c["titre"])
    for ch, d in vus.get(k, []):
        if ch == c["chaine_id"] or abs(d - c["duree"]) <= 3:
            return True
    vus.setdefault(k, []).append((c["chaine_id"], c["duree"]))
    return False


def categoriser(titre, description, cfg):
    """Catégorie au meilleur score (mots du titre x3, de la description x1). Égalité : ordre du config."""
    t, d = sans_accents(titre), sans_accents((description or "")[:300])
    meilleur, score_max = "divers", 0
    for cle, c in cfg["categories"].items():
        mots = _normaliser_mots(c["mots"])
        score = 3 * sum(m in t for m in mots) + sum(m in d for m in mots)
        if score > score_max:
            meilleur, score_max = cle, score
    return meilleur


def age(titre, cfg):
    t = sans_accents(titre)
    return next((a for a, mots in cfg["ages"].items() if any(m in t for m in _normaliser_mots(mots))), "tous")


def chanson(v, cfg):
    s = v["snippet"]
    titre = html.unescape(s["title"]).strip()
    return {"id": v["id"], "titre": titre, "chaine": html.unescape(s.get("channelTitle", "")),
            "chaine_id": s.get("channelId"), "duree": duree_secondes(v["contentDetails"]["duration"]),
            "categorie": categoriser(titre, s.get("description"), cfg), "age": age(titre, cfg),
            "miniature": f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg",
            "pour_enfants": v.get("status", {}).get("madeForKids")}


def lire_csv(chemin):
    brut = Path(chemin).read_bytes()
    try:
        texte = brut.decode("utf-8-sig")
    except UnicodeDecodeError:
        texte = brut.decode("cp1252")  # Excel enregistre parfois en cp1252
    return list(csv.DictReader(texte.splitlines(), delimiter=";"))


# ---------- commandes ----------
def decouvrir(cfg, yt):
    trouvees = {}
    for cat, c in cfg["categories"].items():
        for q in c["recherches"]:
            print(f"Recherche : {q}")
            r = yt.appel("search", 100, part="snippet", type="channel", q=q, maxResults=15,
                         regionCode=cfg["region"], relevanceLanguage=cfg["langue"])
            for it in r.get("items", []):
                cid = it["snippet"]["channelId"]
                x = trouvees.setdefault(cid, {"nom": it["snippet"]["title"], "cats": set(), "hits": 0})
                x["cats"].add(c["nom"])
                x["hits"] += 1
    ids = list(trouvees)
    for i in range(0, len(ids), 50):
        r = yt.appel("channels", 1, part="statistics", id=",".join(ids[i:i + 50]))
        for it in r.get("items", []):
            st = it["statistics"]
            trouvees[it["id"]].update(abonnes=st.get("subscriberCount", ""), videos=st.get("videoCount", ""))
    anciennes = {}
    csv_path = DATA / "chaines_trouvees.csv"
    if csv_path.exists():  # on garde les « x » déjà posés
        anciennes = {r["id"]: r.get("approuvee", "") for r in lire_csv(csv_path)}
    lignes = sorted(trouvees.items(), key=lambda kv: (-kv[1]["hits"], -int(kv[1].get("abonnes") or 0)))
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["approuvee", "nom", "abonnes", "videos", "categories", "lien", "id"])
        for cid, x in lignes:
            w.writerow([anciennes.get(cid, ""), x["nom"], x.get("abonnes", ""), x.get("videos", ""),
                        ", ".join(sorted(x["cats"])), f"https://www.youtube.com/channel/{cid}", cid])
    print(f"\n{len(lignes)} chaînes trouvées -> {csv_path}")
    print("Ouvre ce fichier dans Excel, écris x dans la colonne « approuvee » pour les chaînes sûres, enregistre,")
    print("puis lance : python build_catalog.py construire")


def construire(cfg, yt):
    csv_path = DATA / "chaines_trouvees.csv"
    if not csv_path.exists():
        sys.exit("❌ Lance d'abord : python build_catalog.py decouvrir")
    chaines = [r for r in lire_csv(csv_path) if r.get("approuvee", "").strip()]
    if not chaines:
        sys.exit("❌ Aucune chaîne approuvée : mets x dans la colonne « approuvee » du fichier CSV.")
    catalogue, rejets, vus = [], {}, set()
    excl, titres_vus = charger_exclusions(), {}
    for ch in chaines:
        print(f"Chaîne : {ch['nom']}")
        ids, page = [], None
        while len(ids) < cfg["filtres"]["max_videos_par_chaine"]:
            params = dict(part="contentDetails", playlistId=playlist_envois(ch["id"]), maxResults=50)
            if page:
                params["pageToken"] = page
            try:
                r = yt.appel("playlistItems", 1, **params)
            except httpx.HTTPStatusError as e:  # une chaîne en échec ne bloque pas les autres
                print(f"  ⚠ ignorée ({e.response.status_code})")
                break
            ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
            page = r.get("nextPageToken")
            if not page:
                break
        for i in range(0, len(ids), 50):
            r = yt.appel("videos", 1, part="snippet,contentDetails,status", id=",".join(ids[i:i + 50]))
            for v in r.get("items", []):
                if v["id"] in vus:
                    continue
                vus.add(v["id"])
                raison = evaluer(v, cfg, excl)
                if raison:
                    rejets[raison] = rejets.get(raison, 0) + 1
                    continue
                c = chanson(v, cfg)
                if est_doublon(c, titres_vus):
                    rejets["doublon"] = rejets.get("doublon", 0) + 1
                else:
                    catalogue.append(c)
    cats = [{"id": k, "nom": c["nom"], "emoji": c["emoji"]} for k, c in cfg["categories"].items()]
    (DATA / "catalogue.json").write_text(json.dumps(
        {"genere_le": time.strftime("%Y-%m-%d"), "categories": cats, "chansons": catalogue},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "rapport.json").write_text(json.dumps({"rejets": rejets, "quota": yt.quota}, ensure_ascii=False))
    print(f"\n✅ {len(catalogue)} chansons gardées. Rejets : {rejets}. Quota utilisé : {yt.quota}/10000")


def status(cfg, yt):
    f = DATA / "catalogue.json"
    if not f.exists():
        return print("Pas encore de catalogue.")
    d = json.loads(f.read_text(encoding="utf-8"))
    print(f"Catalogue du {d['genere_le']} : {len(d['chansons'])} chansons")
    for c in d["categories"] + [{"id": "divers", "nom": "Divers", "emoji": ""}]:
        n = sum(1 for s in d["chansons"] if s["categorie"] == c["id"])
        print(f"  {c['emoji']} {c['nom']:<12} {n}")


def controle(cfg, yt):
    """Statistiques + échantillon au hasard par catégorie à relire (data/controle.csv)."""
    f = DATA / "catalogue.json"
    if not f.exists():
        sys.exit("❌ Lance d'abord : python build_catalog.py construire")
    d = json.loads(f.read_text(encoding="utf-8"))
    ch = d["chansons"]
    print(f"{len(ch)} chansons. Déclarées « conçues pour les enfants » : "
          f"{sum(1 for c in ch if c.get('pour_enfants')) * 100 // max(1, len(ch))} %")
    print("\nPar catégorie (% déclarées pour enfants) :")
    cats = d["categories"] + [{"id": "divers", "nom": "Divers", "emoji": ""}]
    random.seed(1)
    lignes = []
    for c in cats:
        liste = [x for x in ch if x["categorie"] == c["id"]]
        pk = sum(1 for x in liste if x.get("pour_enfants")) * 100 // max(1, len(liste))
        print(f"  {c['emoji']} {c['nom']:<12} {len(liste):>5}   {pk} %")
        for x in random.sample(liste, min(25, len(liste))):
            lignes.append([c["nom"], x["titre"], x["chaine"], f"https://youtu.be/{x['id']}", x["id"], x["chaine_id"], ""])
    par_chaine = {}
    for x in ch:
        par_chaine[x["chaine"]] = par_chaine.get(x["chaine"], 0) + 1
    print("\nChaînes les plus présentes :")
    for nom, n in sorted(par_chaine.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {n:>4}  {nom}")
    with open(DATA / "controle.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["categorie", "titre", "chaine", "lien", "video", "chaine_id", "probleme"])
        w.writerows(lignes)
    print(f"\n{len(lignes)} chansons à relire -> data\\controle.csv")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["decouvrir", "construire", "status", "controle"])
    args = p.parse_args()
    charger_env()
    if args.cmd not in ("status", "controle") and not os.environ.get("YOUTUBE_API_KEY"):
        sys.exit("❌ YOUTUBE_API_KEY manquante dans le fichier .env")
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    yt = YouTube(os.environ.get("YOUTUBE_API_KEY", ""), cfg["cache_jours"])
    try:
        {"decouvrir": decouvrir, "construire": construire, "status": status, "controle": controle}[args.cmd](cfg, yt)
    except QuotaEpuise as e:
        sys.exit(f"⏸ {e}")


if __name__ == "__main__":
    main()

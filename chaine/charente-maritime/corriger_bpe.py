"""Pré-remplit le fichier de corrections de la BPE (lu par comptages.py --corrections) par géocodage BAN.

Retient les équipements des fonctions (FONCTIONS de comptages.py) mal placés par l'Insee : QUALITE_XY « A »
(acceptable, 100 à 500 m) ou « M » (mauvaise : voie probable ou position aléatoire dans la commune), et ceux sans
coordonnées. Chaque adresse est cherchée dans la Base adresse nationale (api-adresse.data.gouv.fr), limitée à la
commune. Le CSV sort avec lon/lat retenus (statut « BAN ») ou vides (« à placer », proposition dans lon_ban/lat_ban) :
à vérifier dans QGIS (couche de texte délimité, x = lon, y = lat),
corriger ou vider (lon/lat vides = position Insee gardée), puis passer à comptages.py --corrections.

Exemple (île d'Oléron + bassin de Marennes) :
    python corriger_bpe.py --bpe BPE25.parquet --epci 241700624 --epci 241700699 --sortie corrections_bpe.csv
"""
import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request

import pandas as pd

from comptages import FONCTIONS, CLE_CORRECTION, cle_correction

BAN = "https://api-adresse.data.gouv.fr/search/?"


def geocoder(q, citycode):
    """Meilleure réponse BAN (lon, lat, score, type, label) ou None."""
    url = BAN + urllib.parse.urlencode({"q": q, "citycode": citycode, "limit": 1})
    for essai in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                f = json.load(r)["features"]
            break
        except OSError:
            time.sleep(2 * (essai + 1))   # ponytail: 3 essais, la BAN tolère 50 requêtes/s
    else:
        return None
    if not f:
        return None
    p = f[0]["properties"]
    lon, lat = f[0]["geometry"]["coordinates"]
    return lon, lat, p["score"], p["type"], p["label"]


def ecart_m(lon1, lat1, lon2, lat2):
    """Distance approchée en mètres (équirectangulaire, suffisant à cette échelle)."""
    if any(pd.isna(v) for v in (lon1, lat1, lon2, lat2)):
        return ""
    k = 111320
    return round(math.hypot((lon2 - lon1) * k * math.cos(math.radians(lat1)), (lat2 - lat1) * k))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bpe", required=True, help="BPE parquet (insee.fr)")
    ap.add_argument("--epci", action="append", required=True, help="SIREN d'EPCI à traiter (répétable)")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()

    codes = {c for _, _, _, cs in FONCTIONS for c in cs}
    cols = CLE_CORRECTION + ["EPCI", "LIBCOM", "CODPOS", "LONGITUDE", "LATITUDE", "QUALITE_XY", "QUALITE_GEOLOC"]
    df = pd.read_parquet(a.bpe, columns=cols)
    d = df[df.EPCI.isin(a.epci) & df.TYPEQU.isin(codes)]
    d = d[d.QUALITE_XY.isin(["A", "M"]) | d.LONGITUDE.isna()].drop_duplicates(CLE_CORRECTION)
    print(f"{len(d)} équipements à revoir")

    lignes = []
    for _, r in d.iterrows():
        cle = cle_correction(r)
        voie = " ".join(x for x in cle[3:6] if x)
        g = geocoder(f"{voie} {r.CODPOS} {r.LIBCOM}", r.DEPCOM) if voie else None
        lon, lat, score, typ, label = g if g else ("", "", "", "", "")
        ecart = ecart_m(r.LONGITUDE, r.LATITUDE, lon, lat) if g else ""
        # Réponse retenue : numéro ou voie, score ≥ 0,6, à moins de 500 m de l'Insee (au-delà, la BAN s'est trompée de
        # voie ou donne le milieu d'une longue route). Sinon lon/lat vides : à placer à la main, proposition gardée.
        fiable = g and typ in ("housenumber", "street") and score >= 0.6 and (ecart == "" or ecart <= 500)
        lignes.append(dict(zip([c.lower() for c in CLE_CORRECTION], cle), libcom=r.LIBCOM, qualite_insee=r.QUALITE_XY,
                           geoloc_insee=r.QUALITE_GEOLOC, lon_insee=r.LONGITUDE, lat_insee=r.LATITUDE,
                           lon=lon if fiable else "", lat=lat if fiable else "",
                           statut="BAN" if fiable else "à placer", score_ban=score, type_ban=typ, adresse_ban=label,
                           lon_ban=lon, lat_ban=lat, ecart_m=ecart))
    with open(a.sortie, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]))
        w.writeheader()
        w.writerows(lignes)
    print(f"→ {a.sortie} ({sum(1 for l in lignes if l['lon'] != '')} positions BAN retenues sur {len(lignes)})")


if __name__ == "__main__":
    main()

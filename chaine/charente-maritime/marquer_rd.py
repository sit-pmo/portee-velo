"""Marque dans les tuiles du site les arêtes qui sont des routes départementales, d'après la couche du Département.

Source : « Routes Départementales Charente Maritime » (data.gouv.fr, Licence Ouverte, Département de la
Charente-Maritime), cache/geosir-routes.geojson, Lambert 93. Elle fait foi pour les RD dans le 17, comme les
couches CCIO / CCBM pour les pistes. Elle ne donne aucune vitesse.

Drapeau : bit 15 de attr (classe | sens << 3 | vitesse << 5 occupe les bits 0 à 14). Lu par recollerTuiles
(coeur.js) dans P.rd ; sert au mode « Sans départementales », à la case « Départementales » de Portée et aux
croisements (intersections.py).

Règle d'une arête (voies 2 à 7 hors chemins ; pistes, bandes et chemins jamais RD) :
  - dans le 17 (tuiles/contour.json) : milieu à moins de APPARIEMENT_M d'une RD du Département ET même direction
    que le segment de RD le plus proche (moins de ANGLE_MAX_DEG) : une rue qui part d'une RD n'en est pas une ;
  - hors du 17 (marge de 25 km des tuiles) : pas de couche, on garde la règle OSM (tertiaire ou secondaire).
Les identifiants de nœuds et d'arêtes ne changent pas : maillons, diagnostics et croisements restent valides.
À relancer après chaque tuilage (avant intersections.py).

Usage (depuis chaine/charente-maritime/) :
  .venv/Scripts/python.exe -I -X utf8 marquer_rd.py [--tuiles ../../marennes-oleron/tuiles] [--rd cache/geosir-routes.geojson]
"""
import argparse
import gzip
import json
import re
import time
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

ICI = Path(__file__).resolve().parent
BIT_RD = 1 << 15
APPARIEMENT_M = 12          # écart de tracé OSM / Département toléré (mesuré : 414 croisements sur 617 à < 10 m)
ANGLE_MAX_DEG = 30
# « En attente de classement » : pas encore départementale. « Non géré » : surtout des chaussées séparées (suffixe G).
STATUTS_EXCLUS = {"Tronçon en attente de classement"}
CLASSES_POSSIBLES = (2, 3, 4, 5, 7)     # pas 0 piste, 1 bande, 6 chemin (un sentier qui longe la RD n'est pas la RD)
CLASSES_RD_OSM = (3, 7)                 # hors du 17 : secondaire et tertiaire, comme avant ; 4 reste interdite à part


def numero(route):
    """« 17 D0735E1  G » → « D 735 E1 » ; « 17 GI00321 » (giratoire) → « GI 321 »."""
    m = re.search(r"\b(D|GI|G|AD)0*(\d+)(E\d+)?", route or "")
    return f"{m.group(1)} {m.group(2)}" + (f" {m.group(3)}" if m.group(3) else "") if m else ""


def charger_rd(chemin):
    """Segments des RD du Département en Lambert 93 : (STRtree des segments, x1y1x2y2 (k × 4), numéros)."""
    g = json.loads(Path(chemin).read_text("utf-8"))
    segs, nums = [], []
    for f in g["features"]:
        p = f["properties"]
        if not f["geometry"] or p.get("statut_troncon") in STATUTS_EXCLUS:
            continue
        xy = np.asarray(f["geometry"]["coordinates"])[:, :2]
        segs.append(np.hstack([xy[:-1], xy[1:]]))
        nums += [numero(p.get("route"))] * (len(xy) - 1)
    segs = np.vstack(segs)
    arbre = shapely.STRtree(shapely.linestrings(segs.reshape(-1, 2, 2)))
    return arbre, segs, np.array(nums)


def apparier(rd, x1, y1, x2, y2):
    """Pour chaque arête (Lambert 93), index du segment de RD qui la porte, -1 sinon (milieu à APPARIEMENT_M au
    plus et directions à moins de ANGLE_MAX_DEG, sens indifférent)."""
    arbre, segs, _ = rd
    res = np.full(len(x1), -1)
    if not len(x1):
        return res
    ip, iseg = arbre.query_nearest(shapely.points((x1 + x2) / 2, (y1 + y2) / 2), max_distance=APPARIEMENT_M, all_matches=False)
    u = np.column_stack([x2[ip] - x1[ip], y2[ip] - y1[ip]])
    s = segs[iseg]
    v = np.column_stack([s[:, 2] - s[:, 0], s[:, 3] - s[:, 1]])
    nu, nv = np.hypot(*u.T), np.hypot(*v.T)
    cos = np.abs((u * v).sum(1)) / np.maximum(nu * nv, 1e-9)
    ok = (cos >= np.cos(np.radians(ANGLE_MAX_DEG))) | (nu < 0.5)    # arête de quelques décimètres : direction illisible
    res[ip[ok]] = iseg[ok]
    return res


# ---------------------------------------------------------------- tuiles (suite de varints après « PVT1 »)
def varints(o):
    fin = o < 128
    groupe = np.concatenate([[0], np.cumsum(fin)[:-1]])
    debut = np.concatenate([[0], np.flatnonzero(fin)[:-1] + 1])
    rang = np.arange(len(o)) - debut[groupe]
    return np.bincount(groupe, weights=(o & 127).astype(np.float64) * 128.0 ** rang).astype(np.int64)


def ecrire_varints(v):
    out = bytearray(b"PVT1")
    for x in v.tolist():
        while x >= 0x80:
            out.append((x & 0x7F) | 0x80)
            x >>= 7
        out.append(x)
    return gzip.compress(bytes(out), compresslevel=9, mtime=0)    # comme format_tuile.encoder_tuile


def dezz(u):
    return np.where(u % 2 == 1, -(u + 1) // 2, u // 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tuiles", default=str(ICI.parents[1] / "marennes-oleron" / "tuiles"))
    ap.add_argument("--rd", default=str(ICI / "cache" / "geosir-routes.geojson"))
    args = ap.parse_args()
    t0 = time.time()
    dossier = Path(args.tuiles)
    rd = charger_rd(args.rd)
    print(f"RD du Département : {len(rd[1])} segments")
    dep = shapely.geometry.shape(json.loads((dossier / "contour.json").read_text("utf-8"))["geometrie"])
    shapely.prepare(dep)
    vers_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
    index = json.loads((dossier / "tuiles.json").read_text("utf-8"))
    # Positions de tous les nœuds (b peut être dans une tuile voisine) : première passe
    n_total = max(t[2] + t[3] for t in index["tuiles"])
    lon, lat, lues = np.zeros(n_total), np.zeros(n_total), {}
    for tx, ty, base, n in index["tuiles"]:
        if n == 0:
            continue
        v = varints(np.frombuffer(gzip.decompress((dossier / f"{tx}_{ty}.bin").read_bytes()), np.uint8)[4:])
        lon[base:base + n] = np.cumsum(dezz(v[8:8 + n])) / 1e5
        lat[base:base + n] = np.cumsum(dezz(v[8 + n:8 + 2 * n])) / 1e5
        lues[(tx, ty)] = v
    km = {}
    for (tx, ty), v in lues.items():
        base, n, m = int(v[3]), int(v[4]), int(v[5])
        e = v[8 + 2 * n:8 + 2 * n + 4 * m].reshape(m, 4)     # vue : modifier e modifie v
        a = base + np.cumsum(e[:, 0])
        b = a + dezz(e[:, 1])
        attr = e[:, 3] & ~BIT_RD
        cls = attr & 7
        marque = np.zeros(m, bool)
        cand = np.flatnonzero(np.isin(cls, CLASSES_POSSIBLES) & (b >= 0) & (b < n_total))
        if len(cand):
            mlon, mlat = (lon[a[cand]] + lon[b[cand]]) / 2, (lat[a[cand]] + lat[b[cand]]) / 2
            dedans = shapely.contains_xy(dep, mlon, mlat)
            x1, y1 = vers_l93.transform(lon[a[cand]], lat[a[cand]])
            x2, y2 = vers_l93.transform(lon[b[cand]], lat[b[cand]])
            porte = apparier(rd, x1, y1, x2, y2) >= 0
            marque[cand] = np.where(dedans, porte, np.isin(cls[cand], CLASSES_RD_OSM))
        e[:, 3] = attr | np.where(marque, BIT_RD, 0)
        for c in CLASSES_POSSIBLES:
            km[c] = km.get(c, 0) + e[marque & (cls == c), 2].sum() / 1e4
        (dossier / f"{tx}_{ty}.bin").write_bytes(ecrire_varints(v))
    print("km marqués RD par classe OSM (2 rues, 3 secondaire, 4 principale, 5 communale, 7 tertiaire) :",
          {c: round(k) for c, k in km.items()})
    print(f"{len(lues)} tuiles réécrites ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    assert numero("17 D0735E1  G") == "D 735 E1" and numero("17 GI00321") == "GI 321" and numero("17 D0150") == "D 150"
    main()

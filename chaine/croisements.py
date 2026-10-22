"""Croisements du réseau cyclable avec les routes, par blocs de tuiles (phase 5, 16/10/2026).

intersections.py lit tout le graphe d'un coup et écrit un seul fichier : bien pour un département, pas pour la
métropole (86 M nœuds ; le fichier ferait des dizaines de Mo, chargé d'un bloc par la page). Ici, le territoire
est traité par blocs de BLOC × BLOC tuiles, chacun avec la couronne de tuiles voisines (pour suivre les voies et
juger de l'agglomération au bord du bloc) ; même méthode que intersections.py (croisements, enregistrer,
apparier_osm), RD = bit 15 des tuiles (ref OSM, tuiler.py), numéro = ref OSM.

Sortie, dans le dossier des tuiles : crx/<bx>_<by>.json (format de intersections.json) pour chaque bloc qui a des
croisements, et crx/index.json (taille des blocs, liste des blocs). La page ne charge que les blocs de sa fenêtre.

Tags OSM (vitesses, numéros) : une table compacte des routes, triée par tuile, est écrite une fois
(cache/tmp/routes.parquet) ; chaque bloc n'en lit que ses lignes.

Usage (depuis chaine/, après tuiler.py, GeoParquet d'extraire.py présents) :
  python -I -X utf8 croisements.py --tuiles ../tuiles/fr --parquet 'cache/regions/*.parquet'
"""
import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI), str(ICI.parent)]          # -I retire le dossier du script du chemin
import intersections as X                            # noqa: E402
from format_tuile import TERRITOIRES                 # noqa: E402

BLOC = 4                                             # blocs de 4 × 4 tuiles (~40 km) : ~400 fichiers en métropole


def routes_par_tuile(con, parquets, g, chemin):
    """Routes OSM (tags utiles, géométrie) avec la tuile de leur centre, triées par tuile."""
    lt, ht = g["dlon"] * g["cellulesTuile"], g["dlat"] * g["cellulesTuile"]
    hw = ", ".join(repr(h) for h in X.CLASSE_HIGHWAY)
    t0 = time.time()
    con.execute(f"""
        COPY (SELECT tags['highway'] AS hw, tags['ref'] AS ref, tags['maxspeed'] AS ms, tags['zone:maxspeed'] AS zms,
                     tags['source:maxspeed'] AS sms, tags['maxspeed:type'] AS mst, tags['name'] AS nom,
                     CAST(floor((ST_X(ST_Centroid(geometry)) - {g['lon0']}) / {lt}) AS INTEGER) AS tx,
                     CAST(floor(({g['lat0']} - ST_Y(ST_Centroid(geometry))) / {ht}) AS INTEGER) AS ty,
                     ST_AsWKB(geometry) AS geom
              FROM read_parquet({[str(p) for p in parquets]!r})
              WHERE tags['highway'] IN ({hw}) AND ST_GeometryType(geometry) IN ('LINESTRING', 'POLYGON')
              ORDER BY ty, tx)
        TO '{chemin.as_posix()}' (FORMAT parquet, ROW_GROUP_SIZE 20000)""")
    print(f"routes OSM par tuile : {chemin.stat().st_size / 1024 ** 2:.0f} Mo en {time.time() - t0:.0f} s")


def routes_du_bloc(con, chemin, tx0, tx1, ty0, ty1, vers):
    """(géométries métriques, classes, tags) des routes dont le centre est dans les tuiles [tx0, tx1] × [ty0, ty1]."""
    res = con.execute("""SELECT hw, ref, ms, zms, sms, mst, nom, geom FROM read_parquet(?)
                         WHERE ty BETWEEN ? AND ? AND tx BETWEEN ? AND ?""", [str(chemin), ty0, ty1, tx0, tx1]).fetchall()
    un = lambda t: t[0] if isinstance(t, list) and t else (t or None)   # QuackOSM : map → liste de valeurs
    geoms, classes, tags = [], [], []
    for hw, ref, ms, zms, sms, mst, nom, wkb in res:
        hw0 = un(hw)
        g = shapely.from_wkb(bytes(wkb))
        if g.geom_type == "Polygon":
            g = g.exterior
        xy = np.asarray(g.coords)[:, :2]
        x, y = vers.transform(xy[:, 0], xy[:, 1])
        geoms.append(shapely.linestrings(np.column_stack([x, y])))
        classes.append(X.CLASSE_HIGHWAY.get(hw0, -1))
        tags.append({"highway": hw0, "ref": un(ref), "maxspeed": un(ms), "zone": un(zms) or un(sms) or un(mst), "name": un(nom)})
    return np.array(geoms, dtype=object), np.array(classes), tags


class Tuiles:
    """Tuiles décodées (intersections.lire_tuile), gardées tant que les blocs suivants peuvent en avoir besoin."""

    def __init__(self, dossier, index):
        self.dossier, self.cache = dossier, {}
        self.existe = {(t[0], t[1]): t[3] for t in index["tuiles"]}

    def lire(self, t):
        if t not in self.cache:
            self.cache[t] = X.lire_tuile(self.dossier / f"{t[0]}_{t[1]}.bin") if self.existe.get(t) else None
        return self.cache[t]

    def oublier_avant(self, ty):
        for t in [t for t in self.cache if t[1] < ty]:
            del self.cache[t]


def graphe_local(tuiles, liste):
    """Graphe recollé des tuiles de liste, indices locaux. Renvoie lon, lat, a, b, l, cls, rd et, pour chaque
    tuile, sa plage de nœuds locaux {tuile: (début, fin)}."""
    blocs = sorted(((t, d) for t in liste if (d := tuiles.lire(t)) is not None and len(d[1])), key=lambda x: x[1][0])
    if not blocs:
        return None
    bases = np.array([d[0] for _, d in blocs], np.int64)
    tailles = np.array([len(d[1]) for _, d in blocs], np.int64)
    debuts = np.concatenate([[0], np.cumsum(tailles)])
    lon = np.concatenate([d[1] for _, d in blocs])
    lat = np.concatenate([d[2] for _, d in blocs])

    def local(gid):
        i = np.searchsorted(bases, gid, side="right") - 1
        ok = (i >= 0) & (gid < bases[np.maximum(i, 0)] + tailles[np.maximum(i, 0)])
        return np.where(ok, debuts[np.maximum(i, 0)] + gid - bases[np.maximum(i, 0)], -1)

    a = local(np.concatenate([d[3] for _, d in blocs]))
    b = local(np.concatenate([d[4] for _, d in blocs]))
    l, cls, rd = (np.concatenate([d[k] for _, d in blocs]) for k in (5, 6, 7))
    garde = (a >= 0) & (b >= 0)
    plages = {t: (int(debuts[k]), int(debuts[k + 1])) for k, (t, _) in enumerate(blocs)}
    return lon, lat, a[garde], b[garde], l[garde], cls[garde], rd[garde], plages


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tuiles", required=True, help="dossier des tuiles d'un territoire (ex. ../tuiles/fr)")
    ap.add_argument("--parquet", action="append", default=[], help="GeoParquet d'extraire.py (motif * accepté)")
    ap.add_argument("--bloc", type=int, default=BLOC, help=f"côté des blocs en tuiles (défaut {BLOC})")
    args = ap.parse_args()
    t0 = time.time()
    dossier = Path(args.tuiles)
    index = json.loads((dossier / "tuiles.json").read_text("utf-8"))
    g = index["grille"]
    vers = Transformer.from_crs(4326, TERRITOIRES[g["code"]]["epsg"], always_xy=True)
    fc = dossier / "contour.json"
    contour = shapely.geometry.shape(json.loads(fc.read_text("utf-8"))["geometrie"]) if fc.exists() else None
    parquets = [str(p) for m in args.parquet for p in (sorted(Path().glob(m)) if "*" in m else [Path(m)])]
    con, routes = None, None
    if parquets:
        from tuiler import connecter
        con = connecter()
        routes = ICI / "cache" / "tmp" / f"routes_{g['code']}.parquet"
        routes.parent.mkdir(parents=True, exist_ok=True)
        routes_par_tuile(con, parquets, g, routes)

    B = args.bloc
    tuiles = Tuiles(dossier, index)
    blocs = sorted({(tx // B, ty // B) for tx, ty in tuiles.existe}, key=lambda b: (b[1], b[0]))
    sortie = dossier / "crx"
    shutil.rmtree(sortie, ignore_errors=True)
    sortie.mkdir()
    liste_blocs, total = [], {"rd": 0, "autres": 0, "osm": 0}
    for k, (bx, by) in enumerate(blocs):
        tuiles.oublier_avant(by * B - 1)
        propres = [(tx, ty) for tx in range(bx * B, bx * B + B) for ty in range(by * B, by * B + B) if (tx, ty) in tuiles.existe]
        couronne = [(tx, ty) for tx in range(bx * B - 1, bx * B + B + 1) for ty in range(by * B - 1, by * B + B + 1)]
        gl = graphe_local(tuiles, couronne)
        if gl is None:
            continue
        lon, lat, a, b, l, cls, rd, plages = gl
        cibles = np.zeros(len(lon), bool)
        for t in propres:
            if t in plages:
                cibles[plages[t][0]:plages[t][1]] = True
        liste = X.croisements(lon, lat, a, b, cls, l, cibles)
        if not liste:
            continue
        tags_arete = {}
        if con is not None:
            geoms, classes, tags = routes_du_bloc(con, routes, bx * B - 1, bx * B + B, by * B - 1, by * B + B, vers)
            tags_arete = X.apparier_osm(geoms, classes, tags, vers, lon, lat, a, b, cls,
                                        sorted({e for _n, _t, rs in liste for e, _o in rs}))
        res = X.enregistrer(liste, lon, lat, cls, l, a, b, rd, tags_arete, {}, contour, index.get("genere"), con is not None)
        if not res["lon"]:
            continue
        (sortie / f"{bx}_{by}.json").write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), "utf-8")
        liste_blocs.append([bx, by, len(res["lon"])])
        total["rd"] += res["c"].count(0)
        total["autres"] += res["c"].count(1)
        total["osm"] += res["s"].count(0)
        if k % 25 == 0:
            print(f"bloc {k + 1}/{len(blocs)} ({bx}, {by}) : {len(res['lon'])} croisements ; "
                  f"{sum(n for *_x, n in liste_blocs)} au total ({time.time() - t0:.0f} s)", flush=True)
    (sortie / "index.json").write_text(json.dumps({
        "version": 1, "bloc": B, "genere": datetime.now().strftime("%Y-%m-%d %H:%M"), "osm": index.get("genere"),
        "tags": con is not None, "blocs": liste_blocs}, separators=(",", ":")), "utf-8")
    print(f"Écrit : {len(liste_blocs)} blocs, {total['rd']} croisements avec une RD, {total['autres']} avec d'autres "
          f"routes (vitesse OSM : {total['osm']}), en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

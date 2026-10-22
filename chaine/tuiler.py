"""Étape 2 de la chaîne France : GeoParquet OSM → tuiles de graphe vélo (format v1, format_tuile.py).

Reprend les règles de la carte Marennes-Oléron (classer() de ../build_graph_velo.py : classes,
vitesses, sens uniques). Avec --reseau, sur Marennes-Oléron, le réseau consolidé des collectivités
(CCIO + CCBM, consolider_reseau.py) remplace les pistes OSM, comme build_graph_velo.py --reseau
(integrer_reseau : croisements, extrémités raccordées à 25 m). Pas de traitement spécial du pont.

Étapes :
  1. tuiles à produire = toutes celles qui touchent une des emprises demandées ;
  2. voies cyclables lues dans le GeoParquet (DuckDB + extension spatial), classées ;
  3. nœuds = points de tracé identiques au 1e-7 degré près (les voies OSM qui se croisent partagent
     leurs nœuds ; un pont qui passe au-dessus n'en partage pas) ; segments de plus de 2 km découpés ;
  4. on ne garde que les arêtes dont les deux nœuds sont dans les tuiles, puis, par groupe de tuiles
     contiguës, la plus grande composante connexe (comme build_graph_velo.py, mais une par zone) ;
  5. numérotation globale : tuile par tuile, nœuds triés par carré de 1 km (ordre spatial → petits deltas) ;
  6. cellules de 100 m : terre = polygones terrestres OSM (osmdata.openstreetmap.de) moins natural=water
     de plus de 1 ha ; nœud le plus proche et distance pour chaque mode ;
  7. écriture des tuiles {tx}_{ty}.bin et de l'index tuiles.json.

Usage (depuis departement17/) :
  .venv/Scripts/python.exe -I -X utf8 tuiler.py --parquet cache/poitou-charentes-261008.parquet \\
      --emprise -1.45,45.75,-0.95,46.1 --sortie tuiles
"""
import argparse
import json
import math
import pickle
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pyogrio
import shapely
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI), str(ICI.parent)]   # -I retire le dossier du script du chemin
from lieux import depart_par_defaut, formes, nettoyer_contour  # noqa: E402  textes et départ de la page
from build_graph_velo import (CLASSES_AMEN, CLES_CYCLEWAY, TAMPON_CONTOUR_M,  # noqa: E402  règles communes
                              classer, integrer_reseau)                          # avec la carte Oléron
# Classes fines (11/10/2026, décision D1) : la classe 2 de classer() est découpée selon highway pour que la
# page regroupe librement en niveaux de confort sans re-tuiler. 3 bits de classe dans attr : 5 à 7 étaient libres.
CLASSE_FINE = {"unclassified": 5, "track": 6, "path": 6, "footway": 6, "pedestrian": 6, "bridleway": 6,
               "tertiary": 7, "tertiary_link": 7}   # le reste (residential, living_street, service, road) reste en 2


def classer_fin(tags):
    """classer() de la carte Oléron, puis classe 2 découpée : 2 rues/service, 5 unclassified, 6 chemins, 7 tertiary."""
    hw = tags.get("highway")
    if hw in ("trunk", "trunk_link") and tags.get("bicycle") in VELO_OK_TRUNK:
        tags = {**tags, "highway": "primary" if hw == "trunk" else "primary_link"}
    r = classer(tags)
    if r is None or r[0] != 2:
        return r
    return (CLASSE_FINE.get(tags.get("highway"), 2), *r[1:])


from format_tuile import CELLULES_TUILE, DLAT, DLON, EPSG, LAT0, LON0, TERRITOIRES, choisir, encoder_tuile  # noqa: E402

DOSSIER_EXT_DUCKDB = ICI / "cache" / "duckdb_ext"     # extensions DuckDB sur D:, pas dans ~/.duckdb (C:)
TERRE_ZIP = ICI / "cache" / "land-polygons-split-4326.zip"
TERRE_SHP = "/vsizip/{}/land-polygons-split-4326/land_polygons.shp"
SEGMENT_MAX_M = 2000           # au-delà, le segment est découpé (champ l en Uint16 dm, contre-sens à 5 km/h)
EAU_MIN_M2 = 10_000
MER_PRO_MAX_M = 300            # cellule en mer : garde son nœud s'il est à moins de 300 m (ponts, jetées)            # plans d'eau de plus de 1 ha retirés de la terre
CARRE_TRI_DEG = (0.0125, 0.009)  # carré de ~1 km pour l'ordre spatial des nœuds dans une tuile
M_PAR_DEG = 111_320
# Routes départementales (bit 15 de attr, lu par coeur.js dans P.rd : mode « Sans départementales », case
# « Départementales », croisements) : tag ref d'OSM (« D 735 », « RD 41 », « D 12;D 45 »), sur les rues et routes
# seulement (pistes, bandes et chemins jamais RD), comme marquer_rd.py. La BD TOPO (spec §3) viendra le préciser.
CLASSES_RD = (2, 3, 4, 5, 7)
REF_RD = re.compile(r"(?:^|;)\s*R?D\s?\d", re.IGNORECASE)


def est_departementale(tags):
    """Route départementale d'après OSM : ref commençant par D (ou RD) suivi d'un numéro."""
    return bool(REF_RD.search(tags.get("ref") or ""))


# Routes nationales (17/10/2026, bit 14 de attr → P.nat : mode « Sans départementales et nationales », case
# « Départementales et nationales » ; pas les croisements, qui ne lisent que le bit 15) : ref « N 137 », « RN 7 ».
# Le bit 14 est libre : les vitesses (bits 5 à 14) ne dépassent pas 20 km/h, soit 5 bits.
REF_N = re.compile(r"(?:^|;)\s*R?N\s?\d", re.IGNORECASE)


def code_route(classe, tags):
    """1 départementale, 2 nationale (les deux : départementale), 0 sinon ; rues et routes seulement."""
    if classe not in CLASSES_RD:
        return 0
    return 1 if est_departementale(tags) else 2 if REF_N.search(tags.get("ref") or "") else 0


# Voies express (highway=trunk) : exclues par classer() (vélo interdit en général), sauf ouvertes au vélo dans OSM
# (bicycle=yes…, 17/10/2026) : traitées alors comme une route principale (classe 4).
VELO_OK_TRUNK = {"yes", "designated", "permissive"}


ILE_MIN_M = 5_000              # réseau séparé gardé s'il totalise 5 km de voies (îles sans pont), sinon écarté


def connecter():
    """Connexion DuckDB avec l'extension spatial chargée depuis D:."""
    c = duckdb.connect(config={"extension_directory": str(DOSSIER_EXT_DUCKDB)})
    c.install_extension("spatial")
    c.load_extension("spatial")
    return c


def tuiles_des_emprises(emprises):
    """Ensemble des tuiles (tx, ty) qui touchent au moins une emprise (lon_min, lat_min, lon_max, lat_max)."""
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    tuiles = set()
    for w, s, e, n in emprises:
        for tx in range(int((w - LON0) // lt), int((e - LON0) // lt) + 1):
            for ty in range(int((LAT0 - n) // ht), int((LAT0 - s) // ht) + 1):
                tuiles.add((tx, ty))
    return tuiles


def tuiles_du_contour(chemin, marge_km):
    """Tuiles qui touchent un contour GeoJSON (FeatureCollection, ou contour.json de ce dépôt ; WGS84) élargi de
    marge_km : la métropole sans la mer ni les pays voisins (tuiles/departements.json)."""
    gj = json.loads(Path(chemin).read_text("utf-8"))
    geoms = [shapely.geometry.shape(f["geometry"]) for f in gj["features"]] if "features" in gj \
        else [shapely.geometry.shape(gj.get("geometrie", gj))]
    zone = shapely.union_all(shapely.make_valid(geoms))
    w, s, e, n = zone.bounds
    zone = zone.buffer(marge_km / (111.32 * math.cos(math.radians((s + n) / 2))))   # en degrés, à peu près
    shapely.prepare(zone)
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    return {(tx, ty) for tx, ty in tuiles_des_emprises([zone.bounds])
            if zone.intersects(shapely.box(LON0 + tx * lt, LAT0 - (ty + 1) * ht, LON0 + (tx + 1) * lt, LAT0 - ty * ht))}


def zone_wkt(boites):
    """Union de boîtes (lon_min, lat_min, lon_max, lat_max) en WKT, pour filtrer DuckDB en une seule requête.
    Au-delà de 100 boîtes, leur rectangle englobant : l'union de milliers de tuiles (métropole) est un polygone
    de milliers de sommets, très lent à tester sur chaque voie ; ce qui est lu en trop est écarté ensuite
    (nœuds hors des tuiles, plans d'eau hors des cellules), le résultat ne change pas."""
    if len(boites) > 100:
        return shapely.box(min(b[0] for b in boites), min(b[1] for b in boites),
                           max(b[2] for b in boites), max(b[3] for b in boites)).wkt
    return shapely.union_all([shapely.box(*b) for b in boites]).wkt


def prefiltrer(con, parquets, boites, chemin):
    """Une seule lecture des GeoParquet régionaux : voies et plans d'eau des zones demandées vers un petit
    GeoParquet sur D: (cache/tmp). Évite de relire 2,2 Go pour chaque groupe de tuiles."""
    t0 = time.time()
    con.execute(f"""
        COPY (SELECT feature_id, tags, geometry FROM read_parquet({[str(p) for p in parquets]!r})
              WHERE ((tags['highway'] IS NOT NULL AND ST_GeometryType(geometry) IN ('LINESTRING', 'POLYGON'))
                  OR (tags['natural'] = 'water' AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')))
                AND ST_Intersects(geometry, ST_GeomFromText('{zone_wkt(boites)}')))
        TO '{chemin.as_posix()}' (FORMAT parquet)""")
    print(f"  préfiltrage : {chemin.stat().st_size / 1024 ** 2:.0f} Mo en {time.time() - t0:.0f} s")


def lire_voies(con, source, boites):
    """Voies cyclables classées d'une zone : liste de (coords lon/lat ndarray, classe, vitesse, sens, route).
    route = classe de la voie sans ses tags de bande cyclable (bandes seulement, sinon -1) : sur le territoire
    des collectivités, une bande OSM redevient la route qui la porte (retirer la bande couperait la route)."""
    voies, ecartees = [], 0
    res = con.execute("""
        SELECT DISTINCT ON (feature_id) feature_id, tags, ST_AsWKB(geometry) FROM read_parquet(?)
        WHERE tags['highway'] IS NOT NULL AND ST_GeometryType(geometry) IN ('LINESTRING', 'POLYGON')
          AND ST_Intersects(geometry, ST_GeomFromText(?))
        ORDER BY feature_id""", [source, zone_wkt(boites)]).fetchall()   # ordre fixe : tuiles identiques d'une génération à l'autre
    for _fid, tags, wkb in res:
        r = classer_fin(tags)
        if r is None:
            ecartees += 1
            continue
        g = shapely.from_wkb(bytes(wkb))
        if g.geom_type == "Polygon":       # voie fermée (boucle, rond-point) lue comme surface par QuackOSM
            g = g.exterior
        route = -1
        if r[0] == 1:
            rr = classer_fin({k: v for k, v in tags.items() if k not in CLES_CYCLEWAY + ("bicycle_road", "cyclestreet")})
            route = rr[0] if rr else -1
        voies.append((np.asarray(g.coords)[:, :2], *r, route, code_route(r[0], tags)))
    print(f"  {len(voies)} voies cyclables, {ecartees} écartées par classer()")
    return voies


def densifier(xy):
    """Ajoute des points intermédiaires dans les segments de plus de SEGMENT_MAX_M."""
    lon, lat = xy[:, 0], xy[:, 1]
    d = np.hypot(np.diff(lon) * np.cos(np.radians(lat[:-1])), np.diff(lat)) * M_PAR_DEG
    if (d <= SEGMENT_MAX_M).all():
        return xy
    morceaux = []
    for i, di in enumerate(d):
        k = max(1, math.ceil(di / SEGMENT_MAX_M))
        t = np.arange(k)[:, None] / k
        morceaux.append(xy[i] + t * (xy[i + 1] - xy[i]))
    morceaux.append(xy[-1:])
    return np.vstack(morceaux)


def construire_graphe(voies):
    """Nœuds (lon, lat en degrés) et arêtes (a, b, [classe, vitesse, sens, route, rd]) à partir des tracés."""
    pts, ea, attrs = [], [], []
    debut = 0
    for xy, classe, vit, sens, route, *rd in voies:
        xy = densifier(xy)
        n = len(xy)
        pts.append(xy)
        ea.append(np.arange(debut, debut + n - 1))
        attrs.append(np.repeat([[classe, vit, sens, route, rd[0] if rd else 0]], n - 1, axis=0))
        debut += n
    pts = np.vstack(pts)
    # Clé de nœud : coordonnées entières au 1e-7 degré (précision native des PBF)
    cles = np.round(pts * 1e7).astype(np.int64)
    uniques, inverse = np.unique(cles, axis=0, return_inverse=True)
    inverse = inverse.ravel()
    i = np.concatenate(ea)
    a, b = inverse[i], inverse[i + 1]
    attr = np.vstack(attrs)
    garde = a != b                                  # points doublés dans un même tracé
    return uniques / 1e7, a[garde], b[garde], attr[garde]


def integrer_collectivites(noeuds, a, b, attr, coll, vers_l93):
    """Marennes-Oléron : réseau consolidé des collectivités à la place des pistes OSM (build_graph_velo.py --reseau).
    coll = (territoire en Lambert 93 tamponné de TAMPON_CONTOUR_M, chemin du GeoJSON consolidé).
    Renvoie (noeuds, a, b, attr) ; les nœuds créés (croisements, raccords) sont ajoutés à la fin de noeuds."""
    terr, chemin = coll
    x, y = vers_l93.transform(noeuds[:, 0], noeuds[:, 1])
    dedans = shapely.contains_xy(terr, x, y)
    if not dedans.any():
        return noeuds, a, b, attr
    # 1. Arêtes aux deux bouts dans le territoire : pistes OSM retirées, bandes ramenées à leur route
    attr = attr.copy()
    interieur = dedans[a] & dedans[b]
    bande = interieur & (attr[:, 0] == 1)
    attr[bande, 0] = attr[bande, 3]
    garde = ~(interieur & (attr[:, 0] == 0)) & (attr[:, 0] >= 0)
    print(f"  collectivités : {int((interieur & ~garde).sum())} arêtes OSM de pistes retirées, "
          f"{int((bande & garde).sum())} bandes ramenées à leur route")
    a, b, attr = a[garde], b[garde], attr[garde]
    # 2. Sous-graphe autour du territoire (emprise + 1 km) confié à integrer_reseau : seulement lui, pas le
    #    département entier (liste Python d'arêtes, ~300 octets chacune)
    x0, y0, x1, y1 = terr.bounds
    proche = (x >= x0 - 1000) & (x <= x1 + 1000) & (y >= y0 - 1000) & (y <= y1 + 1000)
    sel = proche[a] | proche[b]
    ids = np.unique(np.concatenate([a[sel], b[sel]]))
    coords = {int(i): (float(noeuds[i, 0]), float(noeuds[i, 1])) for i in ids}
    sous = [(int(u), int(v), int(c), int(vt), int(sn), 0, 0, None)
            for u, v, (c, vt, sn, *_r) in zip(a[sel].tolist(), b[sel].tolist(), attr[sel].tolist())]
    vers_wgs = Transformer.from_crs(EPSG, 4326, always_xy=True)
    integrer_reseau(chemin, sous, coords, lambda nom: 0, vers_l93, vers_wgs)
    # 3. Nœuds créés (ids négatifs -1, -2, …) numérotés à la suite des nœuds existants
    nouveaux = sorted((k for k in coords if k < 0), reverse=True)
    num = {k: len(noeuds) + i for i, k in enumerate(nouveaux)}
    if nouveaux:
        noeuds = np.vstack([noeuds, np.array([coords[k] for k in nouveaux], dtype=float)])
    sa = np.array([num.get(e[0], e[0]) for e in sous], dtype=np.int64)
    sb = np.array([num.get(e[1], e[1]) for e in sous], dtype=np.int64)
    sattr = np.array([[e[2], e[3], e[4], -1, 0] for e in sous], dtype=np.int64).reshape(-1, attr.shape[1])
    return noeuds, np.concatenate([a[~sel], sa]), np.concatenate([b[~sel], sb]), np.vstack([attr[~sel], sattr])


EPCI_OLERON = "241700624"           # CdC de l'île d'Oléron (les 6 autres communes : CC du bassin de Marennes)


def ecrire_territoires(gj, sortie, masque, con, source):
    """territoires.json pour les vues ?territoire= de la page : pmo (14 communes) et oleron (île seule).
    Pour chacun : contour simplifié à ~20 m (dessin, départ dans le territoire) et masque des cellules comptées
    dans le % de couverture = centre dans le contour exact ET sur la terre (même règle que test/non_regression.js ;
    sans la terre, les marais et claires en eau resteraient au dénominateur et le % plafonnerait sous 100).
    Masque sur la grille des tuiles : origine (i0, j0) en cellules, nx × ny, codé par plages alternées 0 / 1."""
    def territoire(features, nom, chapeau):
        exact = shapely.union_all([shapely.geometry.shape(f["geometry"]) for f in features])
        w, s_, e, n = exact.bounds
        i0, j0 = math.floor((w - LON0) / DLON), math.floor((LAT0 - n) / DLAT)
        nx, ny = math.ceil((e - LON0) / DLON) - i0, math.ceil((LAT0 - s_) / DLAT) - j0
        ii, jj = np.meshgrid(np.arange(nx), np.arange(ny))
        clon = (LON0 + (i0 + ii + 0.5) * DLON).ravel()
        clat = (LAT0 - (j0 + jj + 0.5) * DLAT).ravel()
        shapely.prepare(exact)
        m = shapely.contains_xy(exact, clon, clat) & masque.cellules(clon, clat)
        bords = np.flatnonzero(np.diff(np.concatenate([[0], m.astype(np.int8), [0]])))   # débuts / fins des plages de 1
        plages, avant = [], 0
        for deb, fin in zip(bords[::2], bords[1::2]):
            plages += [int(deb - avant), int(fin - deb)]
            avant = fin
        simple = shapely.set_precision(exact.simplify(0.0002), 1e-5)
        print(f"  territoire {nom} : {int(m.sum())} cellules de terre")
        return {"nom": nom, "chapeau": chapeau, "geometrie": json.loads(shapely.to_geojson(simple)),
                "masque": {"i0": i0, "j0": j0, "nx": nx, "ny": ny, "total": int(m.sum()), "plages": plages}}
    feats = gj["features"]
    exact = shapely.union_all([shapely.geometry.shape(f["geometry"]) for f in feats])
    masque.charger_eaux(con, source, [exact.bounds])         # plans d'eau du territoire (quel que soit le groupe en cours)
    territoires = {
        "pmo": territoire(feats, "Marennes-Oléron", "du Pôle Marennes-Oléron (île d'Oléron et bassin de Marennes)"),
        "oleron": territoire([f for f in feats if f["properties"].get("codeEpci") == EPCI_OLERON], "l'île d'Oléron", "de l'île d'Oléron"),
    }
    (sortie / "territoires.json").write_text(json.dumps(territoires, ensure_ascii=False, separators=(",", ":")), "utf-8")


def tuile_xy(lon, lat):
    """Tuile (tx, ty) de chaque point (tableaux)."""
    return (np.floor((lon - LON0) / (DLON * CELLULES_TUILE)).astype(np.int64),
            np.floor((LAT0 - lat) / (DLAT * CELLULES_TUILE)).astype(np.int64))


def groupes_de_tuiles(tuiles):
    """Groupes de tuiles contiguës (4-voisinage) : chaque zone demandée garde sa propre composante."""
    groupe, n = {}, 0
    for t0 in sorted(tuiles):
        if t0 in groupe:
            continue
        pile = [t0]
        groupe[t0] = n
        while pile:
            tx, ty = pile.pop()
            for v in ((tx + 1, ty), (tx - 1, ty), (tx, ty + 1), (tx, ty - 1)):
                if v in tuiles and v not in groupe:
                    groupe[v] = n
                    pile.append(v)
        n += 1
    return groupe


def longueurs_m(noeuds, a, b):
    """Longueur de chaque arête (a, b) en mètres (approximation équirectangulaire, comme l'écriture des tuiles)."""
    return np.hypot((noeuds[a, 0] - noeuds[b, 0]) * np.cos(np.radians(noeuds[a, 1])), noeuds[a, 1] - noeuds[b, 1]) * M_PAR_DEG


def composantes_iles(comp, a, long_m):
    """Composantes dont le réseau totalise au moins ILE_MIN_M : îles sans pont (Marie-Galante, Les Saintes,
    Belle-Île, Petite-Terre…), gardées en plus de la principale ; les petits bouts isolés restent écartés."""
    total = np.bincount(comp[a], weights=long_m, minlength=comp.max() + 1 if len(comp) else 0)
    return set(np.nonzero(total >= ILE_MIN_M)[0].tolist())


def garder_composantes(nb, a, b, groupe_noeud, long_m):
    """Masque des arêtes à garder : par groupe de tuiles, la plus grande composante connexe, plus les réseaux
    séparés d'au moins ILE_MIN_M (îles)."""
    m = coo_matrix((np.ones(len(a)), (a, b)), shape=(nb, nb))
    _, comp = connected_components(m, directed=False)
    utilises = np.zeros(nb, bool)
    utilises[a] = utilises[b] = True
    garder_comp = composantes_iles(comp, a, long_m)
    for g in np.unique(groupe_noeud[utilises]):
        dans = utilises & (groupe_noeud == g)
        valeurs, effectifs = np.unique(comp[dans], return_counts=True)
        garder_comp.add(valeurs[np.argmax(effectifs)])
        print(f"  zone {g} : {len(valeurs)} composantes, la principale regroupe {effectifs.max() / effectifs.sum():.1%} "
              f"des nœuds ; {len(garder_comp)} gardées (îles de {ILE_MIN_M / 1000:g} km de voies ou plus)")
    ok = np.isin(comp, list(garder_comp))
    return ok[a] & ok[b]


class MasqueTerre:
    """Terre = polygones terrestres OSM moins plans d'eau de plus de 1 ha.

    Les polygones terrestres sont lus UNE fois pour toute l'emprise (une lecture du shapefile zippé prend
    ~11 s) ; les plans d'eau sont chargés groupe par groupe (charger_eaux) : ceux de la France entière ne
    tiendraient pas en mémoire.
    """

    def __init__(self, emprise, vers_l93):
        self.terre = pyogrio.read_dataframe(TERRE_SHP.format(TERRE_ZIP.as_posix()), bbox=emprise).geometry.values
        self.arbre_terre = shapely.STRtree(self.terre)
        self.vers_l93 = vers_l93
        self.eaux, self.arbre_eaux = np.array([], dtype=object), None
        print(f"  masque terre : {len(self.terre)} polygones terrestres")

    def charger_eaux(self, con, source, boites):
        """Plans d'eau de plus de 1 ha du groupe de tuiles courant."""
        brut = [shapely.from_wkb(bytes(r[0])) for r in con.execute("""
            SELECT ST_AsWKB(geometry) FROM read_parquet(?)
            WHERE tags['natural'] = 'water' AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')
              AND ST_Intersects(geometry, ST_GeomFromText(?))""", [source, zone_wkt(boites)]).fetchall()]
        aire = lambda g: shapely.transform(g, lambda c: np.column_stack(self.vers_l93.transform(c[:, 0], c[:, 1]))).area
        self.eaux = np.array([g for g in brut if aire(g) > EAU_MIN_M2], dtype=object)
        self.arbre_eaux = shapely.STRtree(self.eaux) if len(self.eaux) else None

    def cellules(self, clon, clat):
        """Booléen « sur la terre » pour chaque centre de cellule d'une tuile."""
        boite = shapely.box(clon.min(), clat.min(), clon.max(), clat.max())
        terre = self.terre[self.arbre_terre.query(boite)]
        if len(terre) == 0:
            return np.zeros(len(clon), bool)
        sol = shapely.union_all(terre)
        if self.arbre_eaux is not None:
            eaux = self.eaux[self.arbre_eaux.query(boite)]
            if len(eaux):
                sol = sol.difference(shapely.union_all(eaux))
        shapely.prepare(sol)
        return shapely.contains_xy(sol, clon, clat)


def ecrire_contour_departement(con, parquets, code, marge_km, sortie, vers_metres):
    """Contour OSM du département (boundary=administrative, admin_level=6, ref=code) : écrit contour.json
    (GeoJSON simplifié, pour la page : limite du survol ; nom et ses formes, départ par défaut, marge,
    voir lieux.py) et renvoie l'emprise élargie de marge_km."""
    src = [str(p) for p in parquets]
    r = con.execute(f"""
        SELECT tags['name'], ST_AsWKB(geometry) FROM read_parquet({src!r})
        WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '6' AND tags['ref'] = ?
          AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON') LIMIT 1""", [code]).fetchone()
    if r:
        nom, geom = r[0], shapely.from_wkb(bytes(r[1]))
    else:
        # DROM (département et région à la fois) : la relation peut porter un autre niveau ou ref:INSEE ; à défaut,
        # union des communes du département (comme chefs_lieux.py pour le Rhône ou la Marne)
        r = con.execute(f"""
            SELECT tags['name'], ST_AsWKB(geometry) FROM read_parquet({src!r})
            WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] IN ('4', '6')
              AND (tags['ref'] = ? OR tags['ref:INSEE'] = ?)
              AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON') ORDER BY tags['admin_level'] DESC LIMIT 1""",
            [code, code]).fetchone()
        if r:
            nom, geom = r[0], shapely.from_wkb(bytes(r[1]))
        else:
            communes = [shapely.from_wkb(bytes(c[0])) for c in con.execute(f"""
                SELECT ST_AsWKB(geometry) FROM read_parquet({src!r})
                WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '8' AND starts_with(tags['ref:INSEE'], ?)
                  AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')""", [code]).fetchall()]
            if not communes:
                sys.exit(f"département {code} absent des GeoParquet fournis")
            nom, geom = TERRITOIRES.get(code, {}).get("nom", code), shapely.union_all(shapely.make_valid(communes))
            print(f"  contour du {code} reconstitué à partir de {len(communes)} communes")
    sortie.mkdir(parents=True, exist_ok=True)
    simple = shapely.simplify(geom, 0.0005)                  # ~50 m : assez pour tester « dans le département »
    simple = nettoyer_contour(simple, vers_metres)           # sans les rochers devenus carrés de 4 points
    depart = depart_par_defaut(con, parquets, code, geom)
    (sortie / "contour.json").write_text(json.dumps({
        "code": code, "nom": nom, **formes(code, nom), "margeKm": marge_km, "depart": depart,
        "geometrie": shapely.geometry.mapping(simple)}, ensure_ascii=False), "utf-8")
    w, s, e, n = geom.bounds
    dlat = marge_km / 111.32
    dlon = marge_km / (111.32 * math.cos(math.radians((s + n) / 2)))
    print(f"Département {code} ({nom}) : contour écrit, emprise élargie de {marge_km:g} km ; départ par défaut : "
          + (f"{depart['commune']} ({depart['choix']})" if depart else "aucune commune trouvée, centre du contour"))
    return (w - dlon, s - dlat, e + dlon, n + dlat)


def ecrire_contour_zone(chemin, code, marge_km, sortie, vers_metres):
    """contour.json d'un territoire tuilé avec --contour (métropole) : contour_<code>.json s'il existe, sinon union
    des contours simplifiée à ~500 m ;
    nom de la grille (TERRITOIRES), départ par défaut = chef-lieu de Paris, departs = les 96 chefs-lieux
    (chefs_lieux.json), parmi lesquels la page tire au sort."""
    gj = json.loads(Path(chemin).read_text("utf-8"))
    geoms = [shapely.geometry.shape(f["geometry"]) for f in gj["features"]] if "features" in gj \
        else [shapely.geometry.shape(gj.get("geometrie", gj))]
    zone = nettoyer_contour(shapely.simplify(shapely.union_all(shapely.make_valid(geoms)), 0.005), vers_metres)
    # Métropole : contour fin tiré des communes s'il existe (contour_metropole.py, 20/10/2026) ; l'union simplifiée
    # ci-dessus coupait les caps et les ports, et la page y refusait le départ
    fin = ICI / f"contour_{code}.json"
    if fin.exists():
        zone = shapely.geometry.shape(json.loads(fin.read_text("utf-8")))
    nom = TERRITOIRES[code]["nom"]
    chefs = ICI / "chefs_lieux.json"
    paris = next((c for c in json.loads(chefs.read_text("utf-8")) if c["dep"] == "75"), None) if chefs.exists() else None
    depart = {k: paris[k] for k in ("commune", "lon", "lat", "choix")} if paris else None
    # La page tire au sort l'un de ces départs à chaque visite (mairies des chefs-lieux des 96 départements)
    departs = [{k: c[k] for k in ("dep", "departement", "commune", "lon", "lat")}
               for c in json.loads(chefs.read_text("utf-8"))] if chefs.exists() else []
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "contour.json").write_text(json.dumps({
        "code": code, "nom": nom, **formes(code, nom), "margeKm": marge_km, "depart": depart, "departs": departs,
        "geometrie": shapely.geometry.mapping(shapely.set_precision(zone, 0.0001))}, ensure_ascii=False), "utf-8")
    print(f"{nom} : contour écrit ({len(getattr(zone, 'geoms', [zone]))} parties)")


def ecrire_controle(chemin, n, a, b, l_dm, attr):
    """Empreinte du graphe avant découpage : le recollage de toutes les tuiles doit la retrouver à l'identique.

    Chaque arête devient "a,b,l,attr" (identifiants globaux) ; lignes triées, sha256 (même calcul dans le test JS).
    """
    import hashlib
    lignes = sorted(f"{x},{y},{z},{w}" for x, y, z, w in zip(a.tolist(), b.tolist(), l_dm.tolist(), attr.tolist()))
    chemin.write_text(json.dumps({"noeuds": n, "aretes": len(lignes),
                                  "empreinte": hashlib.sha256("\n".join(lignes).encode()).hexdigest()}), "utf-8")


def cle_noeud(xy):
    """Clé entière d'un nœud (lon, lat en degrés) : coordonnées au 1e-7 degré, comme construire_graphe()."""
    q = np.round(np.asarray(xy) * 1e7).astype(np.int64)
    return (q[:, 0] << 32) ^ (q[:, 1] & 0xFFFFFFFF)


def garder_composantes_bande(nb, a, b, halo_noeud, long_m):
    """Masque des arêtes à garder dans une bande : la plus grande composante, les îles (composantes_iles), plus
    toute composante qui touche le halo (elle peut rejoindre le réseau principal hors de la bande : la couper
    créerait un trou au raccord)."""
    m = coo_matrix((np.ones(len(a)), (a, b)), shape=(nb, nb))
    _, comp = connected_components(m, directed=False)
    utilises = np.zeros(nb, bool)
    utilises[a] = utilises[b] = True
    valeurs, effectifs = np.unique(comp[utilises], return_counts=True)
    garder = ({valeurs[np.argmax(effectifs)]} | set(np.unique(comp[utilises & halo_noeud]).tolist())
              | composantes_iles(comp, a, long_m))
    print(f"  bande : {len(valeurs)} composantes, {len(garder)} gardées (principale : {effectifs.max() / effectifs.sum():.1%})")
    ok = np.isin(comp, list(garder))
    return ok[a] & ok[b]


def decouper_en_unites(ordre, bande_lignes):
    """Unités de travail : (tuiles, halo, faits) par groupe d'un bloc, ou par bande de bande_lignes lignes
    (halo = lignes voisines, faits = dernière ligne de la bande précédente)."""
    unites = []
    for tg in ordre:
        lignes_g = sorted({t[1] for t in tg})
        if not bande_lignes or len(lignes_g) <= bande_lignes:
            unites.append((set(tg), set(), set()))
            continue
        for i in range(0, len(lignes_g), bande_lignes):
            r0, r1 = lignes_g[i], lignes_g[min(i + bande_lignes, len(lignes_g)) - 1]
            unites.append(({t for t in tg if r0 <= t[1] <= r1}, {t for t in tg if t[1] in (r0 - 1, r1 + 1)},
                           {t for t in tg if t[1] == r0 - 1}))
    return unites


def traiter_groupe(con, source, tuiles, base0, masque, vers_l93, sortie, controle, coll=None,
                   halo=(), faits=(), connus=None, attente=None):
    """Graphe, numérotation (à partir de base0) et écriture des tuiles d'un groupe de tuiles contiguës.
    Renvoie (lignes de l'index, nombre de nœuds, octets écrits, nœuds de bord, tuiles en attente).

    Bandes (métropole, phase 4 : la France d'un bloc demanderait ~50 Go) : `tuiles` = une bande de lignes du
    groupe, `halo` = lignes voisines lues en plus, `faits` = tuiles des bandes déjà écrites, `connus` = leurs
    nœuds de bord (cles, gids, lon, lat, amen), `attente` = tuiles de leur dernière ligne, écrites ici une fois
    leurs cellules complétées par les nœuds de cette bande. Une arête entre la bande et une tuile faite appartient à la bande (retournée au besoin, sens
    1 ↔ 2) et vise le nœud connu de mêmes coordonnées ; une arête vers une tuile pas encore faite est laissée à
    la bande suivante. Sans halo : traitement d'un seul bloc, comme avant.
    """
    tuiles, halo, faits = set(tuiles), set(halo) - set(tuiles), set(faits)
    lues = tuiles | halo
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    boite = lambda tx, ty: (LON0 + tx * lt, LAT0 - (ty + 1) * ht, LON0 + (tx + 1) * lt, LAT0 - ty * ht)
    # Voies lues sur toute l'étendue des tuiles (sinon le bord des tuiles reste sans réseau), halo compris
    voies = lire_voies(con, source, [boite(*t) for t in lues])
    masque.charger_eaux(con, source, [boite(*t) for t in tuiles])
    ordre_tuile = {t: k for k, t in enumerate(sorted(tuiles, key=lambda t: (t[1], t[0])))}
    if voies:
        noeuds, a, b, attr = construire_graphe(voies)
        if coll:
            noeuds, a, b, attr = integrer_collectivites(noeuds, a, b, attr, coll, vers_l93)
        tx, ty = tuile_xy(noeuds[:, 0], noeuds[:, 1])
        couples = list(zip(tx.tolist(), ty.tolist()))
        dans = np.array([t in lues for t in couples], bool)
        propre = np.array([t in tuiles for t in couples], bool)
        garde = dans[a] & dans[b]
        a, b, attr = a[garde], b[garde], attr[garde]
    if not voies or len(a) == 0:
        noeuds, a, b, attr = np.zeros((0, 2)), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 4), np.int64)
        tx = ty = np.zeros(0, np.int64)
        propre = np.zeros(0, bool)
    elif not halo:
        # Plus grande composante connexe du groupe (comme build_graph_velo.py)
        garde = garder_composantes(len(noeuds), a, b, np.zeros(len(noeuds), np.int64), longueurs_m(noeuds, a, b))
        a, b, attr = a[garde], b[garde], attr[garde]
    else:
        garde = garder_composantes_bande(len(noeuds), a, b, dans & ~propre, longueurs_m(noeuds, a, b))
        a, b, attr = a[garde], b[garde], attr[garde]

    # Nœuds du réseau aménagé (pistes, bandes…), marqués avant d'écarter les arêtes laissées à la bande suivante
    amen_g = np.zeros(len(noeuds), bool)
    est_amen = np.isin(attr[:, 0], CLASSES_AMEN) if len(a) else np.zeros(0, bool)
    amen_g[a[est_amen]] = amen_g[b[est_amen]] = True

    # Arêtes de la bande : a dans la bande ; b dans la bande ou dans une tuile déjà faite (nœud connu)
    externe = np.zeros(len(a), bool)                        # b dans une tuile déjà faite
    manques = 0
    if halo and len(a):
        fait = np.array([t in faits for t in couples], bool)
        inv = ~propre[a] & propre[b]                       # arête vue depuis l'autre bout : on la retourne
        a[inv], b[inv] = b[inv].copy(), a[inv].copy()
        s = attr[inv, 2]
        attr[inv, 2] = np.where(s == 1, 2, np.where(s == 2, 1, s))
        # Nœuds de la bande : extrémités de toutes ses arêtes, y compris celles laissées à la bande suivante
        # (qui y retrouvera ces nœuds par leurs coordonnées)
        ext = np.concatenate([a[propre[a]], b[propre[b]]])
        externe = propre[a] & fait[b]
        garde = propre[a] & (propre[b] | fait[b])
        a, b, attr, externe = a[garde], b[garde], attr[garde], externe[garde]
        gid_ext = np.full(len(a), -1, np.int64)
        if externe.any():
            cles = cle_noeud(noeuds[b[externe]])
            par_cle = dict(zip(connus["cles"].tolist(), connus["gids"].tolist())) if connus else {}
            gid_ext[externe] = [par_cle.get(k, -1) for k in cles.tolist()]
            perdu = externe & (gid_ext < 0)
            manques = int(perdu.sum())
            a, b, attr, externe, gid_ext = a[~perdu], b[~perdu], attr[~perdu], externe[~perdu], gid_ext[~perdu]
        utilises = np.unique(np.concatenate([ext, a]))
    else:
        utilises = np.unique(np.concatenate([a, b]))
        gid_ext = np.full(len(a), -1, np.int64)
    if manques:
        print(f"  raccords : {manques} arêtes sans nœud correspondant dans la bande précédente, écartées")

    # Longueurs avant renumérotation (b peut appartenir à une tuile déjà écrite)
    longueur = np.hypot((noeuds[a, 0] - noeuds[b, 0]) * np.cos(np.radians(noeuds[a, 1])),
                        noeuds[a, 1] - noeuds[b, 1]) * M_PAR_DEG if len(a) else np.zeros(0)

    # Numérotation : tuile par tuile (ordre ty, tx), puis carré de 1 km, puis longitude ; à partir de base0
    lon, lat = noeuds[utilises, 0], noeuds[utilises, 1]
    k_tuile = np.array([ordre_tuile[(x, y)] for x, y in zip(tx[utilises].tolist(), ty[utilises].tolist())], dtype=np.int64)
    carre_y = np.floor((LAT0 - lat) / CARRE_TRI_DEG[1]).astype(np.int64)
    carre_x = np.floor((lon - LON0) / CARRE_TRI_DEG[0]).astype(np.int64)
    tri = np.lexsort((lon, carre_x, carre_y, k_tuile))
    gid = np.full(len(noeuds), -1, np.int64)
    gid[utilises[tri]] = np.arange(len(utilises))
    a = gid[a]                                              # indices locaux au groupe ; + base0 à l'écriture
    b_loc = np.where(externe, -1, gid[b])                   # -1 : nœud d'une tuile déjà écrite
    b_abs = np.where(externe, gid_ext, b_loc + base0)       # identifiant global
    lon, lat, k_tuile = lon[tri], lat[tri], k_tuile[tri]
    l_dm = np.maximum(1, np.round(longueur * 10)).astype(np.int64)
    classe, vit, sens = attr[:, 0], attr[:, 1], attr[:, 2]
    rd = attr[:, 4] if attr.shape[1] > 4 else np.zeros(len(attr), np.int64)    # code_route : 1 RD, 2 nationale
    assert vit.max(initial=0) < 512, "vitesse sur plus de 9 bits : empiéterait sur le bit 14 (nationales)"
    attr_code = (classe | (sens << 3) | (vit << 5) | ((rd == 2).astype(np.int64) << 14)   # bit 14 : nationale (P.nat)
                 | ((rd == 1).astype(np.int64) << 15))                                     # bit 15 : départementale (P.rd)
    print(f"  graphe : {len(lon)} nœuds, {len(a)} arêtes, {longueur.sum() / 1000:.0f} km")
    if controle is not None:
        controle.append((len(lon), a + base0, b_abs, l_dm, attr_code))

    # Plus proches nœuds des cellules : ceux de la bande, plus les nœuds de bord de la bande précédente (connus,
    # identifiants déjà donnés) ; ids_* = identifiants globaux
    x, y = vers_l93.transform(lon, lat)
    amen_noeud = amen_g[utilises[tri]]
    pts, ids = np.column_stack([x, y]), np.arange(len(lon)) + base0
    if connus:
        cx_c, cy_c = vers_l93.transform(connus["lon"], connus["lat"])
        pts_c = np.column_stack([cx_c, cy_c])
        pts_t, ids_t = np.vstack([pts, pts_c]), np.concatenate([ids, connus["gids"]])
        am_t = np.concatenate([amen_noeud, connus["amen"]])
    else:
        pts_t, ids_t, am_t = pts, ids, amen_noeud
    arbre_tout = (cKDTree(pts_t), ids_t) if len(ids_t) else None
    arbre_amen = (cKDTree(pts_t[am_t]), ids_t[am_t]) if am_t.any() else None
    # Tuiles de la dernière ligne de la bande précédente, en attente : leurs cellules peuvent avoir leur plus
    # proche nœud dans cette bande-ci (on garde le plus proche des deux)
    octets_total = 0
    for t in (attente or []):
        for cle, arbre_ids in (("tout", arbre_tout), ("amen", arbre_amen)):
            d, p_ = proches(arbre_ids, t["pts"])
            mieux = d < t["d_" + cle]
            t["d_" + cle], t["p_" + cle] = np.where(mieux, d, t["d_" + cle]), np.where(mieux, p_, t["p_" + cle])
        octets_total += ecrire_tuile(sortie, t)
    debut_tuile = np.searchsorted(k_tuile, np.arange(len(ordre_tuile) + 1))
    index, en_attente = [], []
    derniere = max(t[1] for t in tuiles)
    differer = bool(halo) and any(t[1] == derniere + 1 for t in halo)   # une bande suit
    ii, jj = np.meshgrid(np.arange(CELLULES_TUILE), np.arange(CELLULES_TUILE))
    for (tx0, ty0), k in sorted(ordre_tuile.items(), key=lambda kv: kv[1]):
        deb, fin = int(debut_tuile[k]), int(debut_tuile[k + 1])
        possedees = (a >= deb) & (a < fin)                   # arête rattachée à la tuile de son nœud a
        clon = (LON0 + (tx0 * CELLULES_TUILE + ii + 0.5) * DLON).ravel()
        clat = (LAT0 - (ty0 * CELLULES_TUILE + jj + 0.5) * DLAT).ravel()
        cx, cy = vers_l93.transform(clon, clat)
        t = {
            "tx": tx0, "ty": ty0, "base": base0 + deb,
            "lon": np.round(lon[deb:fin] * 1e5).astype(np.int64).tolist(),
            "lat": np.round(lat[deb:fin] * 1e5).astype(np.int64).tolist(),
            "a": (a[possedees] - deb).tolist(), "b": b_abs[possedees].tolist(),
            "l": l_dm[possedees].tolist(), "attr": attr_code[possedees].tolist(),
            "ncx": CELLULES_TUILE, "ncy": CELLULES_TUILE, "terre": masque.cellules(clon, clat),
            "pts": np.column_stack([cx, cy]),
        }
        t["d_tout"], t["p_tout"] = proches(arbre_tout, t["pts"])
        t["d_amen"], t["p_amen"] = proches(arbre_amen, t["pts"])
        if differer and ty0 == derniere:
            en_attente.append(t)
        else:
            octets_total += ecrire_tuile(sortie, t)
        index.append([tx0, ty0, base0 + deb, fin - deb])
    # Nœuds de bord pour la bande suivante : ceux de la dernière ligne de la bande (une arête de moins de 2 km
    # ne relie que des tuiles voisines)
    bord = None
    if differer:
        sel = np.array([t[1] == derniere for t in sorted(tuiles, key=lambda t: (t[1], t[0]))], bool)[k_tuile] \
            if len(lon) else np.zeros(0, bool)
        idx = np.nonzero(sel)[0]
        bord = {"cles": cle_noeud(np.column_stack([lon[idx], lat[idx]])), "gids": idx + base0,
                "lon": lon[idx], "lat": lat[idx], "amen": amen_noeud[idx]}
    return index, len(lon), octets_total, bord, en_attente


def proches(arbre_ids, pts):
    """Distance (m) et identifiant global du plus proche nœud de chaque point ; inf et -1 sans nœud."""
    if arbre_ids is None:
        return np.full(len(pts), np.inf), np.full(len(pts), -1, np.int64)
    arbre, ids = arbre_ids
    d, i = arbre.query(pts)
    return d, ids[i]


def ecrire_tuile(sortie, t):
    """Encode et écrit une tuile (cellules : plus proches nœuds déjà choisis) ; renvoie le nombre d'octets.
    Cellules en mer : aucun nœud (allège la tuile ; tempsGrille les ignore de toute façon), sauf à moins
    de MER_PRO_MAX_M d'un nœud : sinon un pont ou une jetée n'est le nœud d'aucune cellule et
    accrocher() ne peut pas s'y raccrocher (curseur sur le pont d'Oléron accroché à la côte à 800 m)."""
    terre = t["terre"]
    p_tout = np.where((terre | (t["d_tout"] <= MER_PRO_MAX_M)) & (t["p_tout"] >= 0), t["p_tout"], -1)
    p_amen = np.where((terre | (t["d_amen"] <= MER_PRO_MAX_M)) & (t["p_amen"] >= 0), t["p_amen"], -1)
    octets = encoder_tuile({**t, "terre": terre.astype(int).tolist(), "pro_tout": p_tout.tolist(),
                            "pro_amen": p_amen.tolist(), "dist_tout": np.minimum(t["d_tout"], 1e6).tolist(),
                            "dist_amen": np.minimum(t["d_amen"], 1e6).tolist()})
    (sortie / f"{t['tx']}_{t['ty']}.bin").write_bytes(octets)
    return len(octets)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", action="append", required=True, help="GeoParquet produit par extraire.py (répétable, motif * accepté)")
    ap.add_argument("--emprise", action="append", default=[], help="lon_min,lat_min,lon_max,lat_max (répétable)")
    ap.add_argument("--grille", default="fr", choices=sorted(TERRITOIRES),
                    help="grille du territoire (fr = métropole, 971 à 976 = DROM)  ; --sortie ../tuiles/<grille>)")
    ap.add_argument("--departement", help="code du département (ex. 17) : emprise = contour OSM + marge, contour écrit dans la sortie")
    ap.add_argument("--marge-km", type=float, default=25, help="marge autour du département (défaut 25 km)")
    ap.add_argument("--contour", help="GeoJSON (ex. ../tuiles/departements.json) : tuiles qui touchent ce contour "
                                      "élargi de --marge-km (métropole, phase 4)")
    ap.add_argument("--chefs-lieux", help="chefs_lieux.json : ajoute une emprise de --rayon-km autour de chaque mairie")
    ap.add_argument("--rayon-km", type=float, default=8, help="demi-côté de l'emprise autour d'une mairie (défaut 8 km)")
    ap.add_argument("--sortie", default=str(ICI.parent / "tuiles"), help="dossier des tuiles (défaut : tuiles/ du site, dossier parent)")
    ap.add_argument("--reseau", help="réseau consolidé des collectivités (GeoJSON EPSG:2154, consolider_reseau.py) : "
                    "remplace les pistes OSM sur le territoire de --territoire")
    ap.add_argument("--territoire", default=str(ICI.parent / "cache" / "contour_territoire.geojson"),
                    help="communes du territoire des collectivités (GeoJSON WGS84 ; défaut : Marennes-Oléron)")
    ap.add_argument("--controle", help="écrit l'empreinte du graphe complet (test de recollage sans perte)")
    ap.add_argument("--duree-max-min", type=float, default=0,
                    help="s'arrête proprement après ce temps (0 = sans limite) ; relancer la même commande reprend")
    ap.add_argument("--bande-lignes", type=int, default=0,
                    help="découpe chaque groupe en bandes de N lignes de tuiles traitées l'une après l'autre "
                         "(mémoire d'une bande ; 0 = groupe d'un bloc). Métropole : ~5")
    args = ap.parse_args()
    global LON0, LAT0, DLON, DLAT, EPSG             # grille du territoire, lue par toutes les fonctions du module
    LON0, LAT0, DLON, DLAT, EPSG = choisir(args.grille)
    t0 = time.time()
    parquets = [str(p) for motif in args.parquet for p in (sorted(Path().glob(motif)) if "*" in motif else [Path(motif)])]
    emprises = [tuple(float(v) for v in e.split(",")) for e in args.emprise]
    con = connecter()
    sortie = Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    if args.departement:
        emprises.append(ecrire_contour_departement(con, parquets, args.departement, args.marge_km, sortie,
                                                   Transformer.from_crs(4326, EPSG, always_xy=True)))
    if args.chefs_lieux:
        for c in json.loads(Path(args.chefs_lieux).read_text("utf-8")):
            dlat = args.rayon_km / 111.32
            dlon = args.rayon_km / (111.32 * math.cos(math.radians(c["lat"])))
            emprises.append((c["lon"] - dlon, c["lat"] - dlat, c["lon"] + dlon, c["lat"] + dlat))
    tuiles_contour = tuiles_du_contour(args.contour, args.marge_km) if args.contour else set()
    if args.contour and not args.departement:
        ecrire_contour_zone(args.contour, args.grille, args.marge_km, sortie, Transformer.from_crs(4326, EPSG, always_xy=True))
    if not emprises and not tuiles_contour:
        ap.error("--emprise, --departement, --contour ou --chefs-lieux requis")
    # La grille part de son coin nord-ouest : une emprise au nord ou à l'ouest donnerait des numéros de tuile négatifs
    if (emprises and (min(e[0] for e in emprises) < LON0 or max(e[3] for e in emprises) > LAT0)) \
            or any(tx < 0 or ty < 0 for tx, ty in tuiles_contour):
        sys.exit(f"emprise hors de la grille {args.grille} (origine {LON0}, {LAT0}) : déplacer l'origine dans format_tuile.py")
    tuiles = tuiles_des_emprises(emprises) | tuiles_contour
    groupe = groupes_de_tuiles(tuiles)
    groupes = {}
    for t, g in groupe.items():
        groupes.setdefault(g, set()).add(t)
    print(f"{len(tuiles)} tuiles en {len(groupes)} groupe(s) de tuiles contiguës")
    vers_l93 = Transformer.from_crs(4326, EPSG, always_xy=True)   # « l93 » : projection métrique du territoire
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    boites = [(LON0 + tx * lt, LAT0 - (ty + 1) * ht, LON0 + (tx + 1) * lt, LAT0 - ty * ht) for tx, ty in tuiles]

    # Plusieurs fichiers ou plusieurs groupes : une seule lecture des sources vers un fichier intermédiaire
    # Reprise : la progression (groupes faits, index, numéro de nœud suivant) est enregistrée après chaque
    # groupe ; une relance avec les mêmes arguments repart du groupe suivant. Signature = arguments.
    signature = json.dumps([sorted(parquets), sorted(map(list, tuiles)), args.reseau, args.bande_lignes])
    fichier_prog = sortie / "progression.json"
    prog = json.loads(fichier_prog.read_text("utf-8")) if fichier_prog.exists() else None
    if not prog or prog["signature"] != signature:
        prog = {"signature": signature, "faits": 0, "index": [], "base": 0, "octets": 0}
    if args.controle and prog["faits"]:
        sys.exit("--controle demande un passage complet : supprimez progression.json")

    source = parquets[0]
    if len(parquets) > 1 or len(groupes) > 1:
        intermediaire = ICI / "cache" / "tmp" / "zones.parquet"
        intermediaire.parent.mkdir(parents=True, exist_ok=True)
        if not (prog["faits"] and intermediaire.exists()):          # déjà préfiltré lors du lancement précédent
            prefiltrer(con, parquets, boites, intermediaire)
        source = str(intermediaire)
    emprise = (min(b[0] for b in boites), min(b[1] for b in boites), max(b[2] for b in boites), max(b[3] for b in boites))
    masque = MasqueTerre(emprise, vers_l93)

    controle = [] if args.controle else None
    coll = None
    if args.reseau:
        gj = json.loads(Path(args.territoire).read_text("utf-8"))
        terr = shapely.union_all([shapely.geometry.shape(f["geometry"]) for f in gj["features"]])
        terr_l93 = shapely.transform(terr, lambda xy: np.column_stack(vers_l93.transform(xy[:, 0], xy[:, 1])))
        terr_l93 = terr_l93.buffer(TAMPON_CONTOUR_M)        # garde les voies littorales un peu hors contour communal
        shapely.prepare(terr_l93)                            # contains_xy sur des millions de nœuds
        coll = (terr_l93, args.reseau)
        print(f"Réseau des collectivités : {args.reseau} sur {len(gj['features'])} communes")
    ordre = sorted(groupes.values(), key=lambda tg: min((t[1], t[0]) for t in tg))
    # Unités de travail : un groupe d'un bloc, ou ses bandes de --bande-lignes lignes (halo = lignes voisines,
    # faits = ligne de la bande précédente, dont les nœuds de bord sont gardés dans bord.npz pour la reprise)
    unites = decouper_en_unites(ordre, args.bande_lignes)
    # État d'une bande à la suivante (nœuds de bord, tuiles en attente), gardé sur disque pour la reprise
    fichier_bord = sortie / "bord.pkl"
    connus, attente = None, []
    if prog["faits"] and fichier_bord.exists():
        connus, attente = pickle.loads(fichier_bord.read_bytes())
    # Unités traitées l'une après l'autre (mémoire = une unité) ; numérotation continue de l'une à l'autre
    for rang in range(prog["faits"], len(unites)):
        if args.duree_max_min and time.time() - t0 > args.duree_max_min * 60:
            print(f"Arrêt après {args.duree_max_min:g} min : {rang}/{len(unites)} unités faites. Relancez la même commande pour continuer.")
            return
        tg, halo, faits = unites[rang]
        print(f"{'Bande' if halo else 'Groupe'} {rang + 1}/{len(unites)} : {len(tg)} tuiles"
              + (f", lignes {min(t[1] for t in tg)} à {max(t[1] for t in tg)}" if halo else ""))
        lignes, nb, octets, connus, attente = traiter_groupe(
            con, source, tg, prog["base"], masque, vers_l93, sortie, controle, coll, halo=halo, faits=faits,
            connus=connus if faits else None, attente=attente if faits else None)
        fichier_bord.write_bytes(pickle.dumps((connus, attente)))
        prog["index"] += lignes
        prog["base"] += nb
        prog["octets"] += octets
        prog["faits"] = rang + 1
        fichier_prog.write_text(json.dumps(prog), "utf-8")
    fichier_bord.unlink(missing_ok=True)
    index, base, octets_total = prog["index"], prog["base"], prog["octets"]
    if args.controle:
        cat = lambda i: np.concatenate([c[i] for c in controle])
        ecrire_controle(Path(args.controle), sum(c[0] for c in controle), cat(1), cat(2), cat(3), cat(4))
    (sortie / "tuiles.json").write_text(json.dumps({
        "version": 1, "genere": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": [Path(p).name for p in parquets], "terre": TERRE_ZIP.name,
        "collectivites": bool(args.reseau),        # réseau CCIO + CCBM intégré sur Marennes-Oléron
        "grille": {"code": args.grille, "lon0": LON0, "lat0": LAT0, "dlon": DLON, "dlat": DLAT, "cellulesTuile": CELLULES_TUILE},
        "tuiles": index,
    }, ensure_ascii=False), "utf-8")
    fichier_prog.unlink()                                        # terminé : plus rien à reprendre
    if args.reseau:
        ecrire_territoires(gj, sortie, masque, con, source)
    print(f"Écrit : {len(index)} tuiles, {base} nœuds, {octets_total / 1024 ** 2:.1f} Mo, "
          f"{octets_total / 1024 / max(1, len(index)):.0f} Ko par tuile en moyenne, en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

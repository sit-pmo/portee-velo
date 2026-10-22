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
from build_graph_velo import (CLASSES_AMEN, CLES_CYCLEWAY, TAMPON_CONTOUR_M,  # noqa: E402  règles communes
                              classer, integrer_reseau)                          # avec la carte Oléron
# Classes fines (11/10/2026, décision D1) : la classe 2 de classer() est découpée selon highway pour que la
# page regroupe librement en niveaux de confort sans re-tuiler. 3 bits de classe dans attr : 5 à 7 étaient libres.
CLASSE_FINE = {"unclassified": 5, "track": 6, "path": 6, "footway": 6, "pedestrian": 6, "bridleway": 6,
               "tertiary": 7, "tertiary_link": 7}   # le reste (residential, living_street, service, road) reste en 2


def classer_fin(tags):
    """classer() de la carte Oléron, puis classe 2 découpée : 2 rues/service, 5 unclassified, 6 chemins, 7 tertiary."""
    r = classer(tags)
    if r is None or r[0] != 2:
        return r
    return (CLASSE_FINE.get(tags.get("highway"), 2), *r[1:])


from format_tuile import CELLULES_TUILE, DLAT, DLON, LAT0, LON0, encoder_tuile  # noqa: E402

DOSSIER_EXT_DUCKDB = ICI / "cache" / "duckdb_ext"     # extensions DuckDB sur D:, pas dans ~/.duckdb (C:)
TERRE_ZIP = ICI / "cache" / "land-polygons-split-4326.zip"
TERRE_SHP = "/vsizip/{}/land-polygons-split-4326/land_polygons.shp"
SEGMENT_MAX_M = 2000           # au-delà, le segment est découpé (champ l en Uint16 dm, contre-sens à 5 km/h)
EAU_MIN_M2 = 10_000
MER_PRO_MAX_M = 300            # cellule en mer : garde son nœud s'il est à moins de 300 m (ponts, jetées)            # plans d'eau de plus de 1 ha retirés de la terre
CARRE_TRI_DEG = (0.0125, 0.009)  # carré de ~1 km pour l'ordre spatial des nœuds dans une tuile
M_PAR_DEG = 111_320


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


def zone_wkt(boites):
    """Union de boîtes (lon_min, lat_min, lon_max, lat_max) en WKT, pour filtrer DuckDB en une seule requête."""
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
        voies.append((np.asarray(g.coords)[:, :2], *r, route))
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
    """Nœuds (lon, lat en degrés) et arêtes (a, b, [classe, vitesse, sens, route]) à partir des tracés."""
    pts, ea, attrs = [], [], []
    debut = 0
    for xy, classe, vit, sens, route in voies:
        xy = densifier(xy)
        n = len(xy)
        pts.append(xy)
        ea.append(np.arange(debut, debut + n - 1))
        attrs.append(np.repeat([[classe, vit, sens, route]], n - 1, axis=0))
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
            for u, v, (c, vt, sn, _r) in zip(a[sel].tolist(), b[sel].tolist(), attr[sel].tolist())]
    vers_wgs = Transformer.from_crs(2154, 4326, always_xy=True)
    integrer_reseau(chemin, sous, coords, lambda nom: 0, vers_l93, vers_wgs)
    # 3. Nœuds créés (ids négatifs -1, -2, …) numérotés à la suite des nœuds existants
    nouveaux = sorted((k for k in coords if k < 0), reverse=True)
    num = {k: len(noeuds) + i for i, k in enumerate(nouveaux)}
    if nouveaux:
        noeuds = np.vstack([noeuds, np.array([coords[k] for k in nouveaux], dtype=float)])
    sa = np.array([num.get(e[0], e[0]) for e in sous], dtype=np.int64)
    sb = np.array([num.get(e[1], e[1]) for e in sous], dtype=np.int64)
    sattr = np.array([[e[2], e[3], e[4], -1] for e in sous], dtype=np.int64).reshape(-1, 4)
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


def garder_composantes(nb, a, b, groupe_noeud):
    """Masque des arêtes à garder : par groupe de tuiles, la plus grande composante connexe."""
    m = coo_matrix((np.ones(len(a)), (a, b)), shape=(nb, nb))
    _, comp = connected_components(m, directed=False)
    utilises = np.zeros(nb, bool)
    utilises[a] = utilises[b] = True
    garder_comp = set()
    for g in np.unique(groupe_noeud[utilises]):
        dans = utilises & (groupe_noeud == g)
        valeurs, effectifs = np.unique(comp[dans], return_counts=True)
        garder_comp.add(valeurs[np.argmax(effectifs)])
        print(f"  zone {g} : {len(valeurs)} composantes, la principale regroupe {effectifs.max() / effectifs.sum():.1%} des nœuds")
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


def ecrire_contour_departement(con, parquets, code, marge_km, sortie):
    """Contour OSM du département (boundary=administrative, admin_level=6, ref=code) : écrit contour.json
    (GeoJSON simplifié, pour la page : limite du survol) et renvoie l'emprise élargie de marge_km."""
    r = con.execute(f"""
        SELECT tags['name'], ST_AsWKB(geometry) FROM read_parquet({[str(p) for p in parquets]!r})
        WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '6' AND tags['ref'] = ?
          AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON') LIMIT 1""", [code]).fetchone()
    if not r:
        sys.exit(f"département {code} absent des GeoParquet fournis")
    nom, geom = r[0], shapely.from_wkb(bytes(r[1]))
    sortie.mkdir(parents=True, exist_ok=True)
    simple = shapely.simplify(geom, 0.0005)                  # ~50 m : assez pour tester « dans le département »
    (sortie / "contour.json").write_text(json.dumps({"code": code, "nom": nom, "geometrie": shapely.geometry.mapping(simple)},
                                                    ensure_ascii=False), "utf-8")
    w, s, e, n = geom.bounds
    dlat = marge_km / 111.32
    dlon = marge_km / (111.32 * math.cos(math.radians((s + n) / 2)))
    print(f"Département {code} ({nom}) : contour écrit, emprise élargie de {marge_km:g} km")
    return (w - dlon, s - dlat, e + dlon, n + dlat)


def ecrire_controle(chemin, n, a, b, l_dm, attr):
    """Empreinte du graphe avant découpage : le recollage de toutes les tuiles doit la retrouver à l'identique.

    Chaque arête devient "a,b,l,attr" (identifiants globaux) ; lignes triées, sha256 (même calcul dans le test JS).
    """
    import hashlib
    lignes = sorted(f"{x},{y},{z},{w}" for x, y, z, w in zip(a.tolist(), b.tolist(), l_dm.tolist(), attr.tolist()))
    chemin.write_text(json.dumps({"noeuds": n, "aretes": len(lignes),
                                  "empreinte": hashlib.sha256("\n".join(lignes).encode()).hexdigest()}), "utf-8")


def traiter_groupe(con, source, tuiles, base0, masque, vers_l93, sortie, controle, coll=None):
    """Graphe, numérotation (à partir de base0) et écriture des tuiles d'un groupe de tuiles contiguës.
    Renvoie (lignes de l'index, nombre de nœuds, octets écrits)."""
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    # Voies lues sur toute l'étendue des tuiles (sinon le bord des tuiles reste sans réseau)
    boites = [(LON0 + tx * lt, LAT0 - (ty + 1) * ht, LON0 + (tx + 1) * lt, LAT0 - ty * ht) for tx, ty in tuiles]
    voies = lire_voies(con, source, boites)
    masque.charger_eaux(con, source, boites)
    ordre_tuile = {t: k for k, t in enumerate(sorted(tuiles, key=lambda t: (t[1], t[0])))}
    if voies:
        noeuds, a, b, attr = construire_graphe(voies)
        if coll:
            noeuds, a, b, attr = integrer_collectivites(noeuds, a, b, attr, coll, vers_l93)
        tx, ty = tuile_xy(noeuds[:, 0], noeuds[:, 1])
        dans = np.array([(x, y) in tuiles for x, y in zip(tx.tolist(), ty.tolist())])
        garde = dans[a] & dans[b]
        a, b, attr = a[garde], b[garde], attr[garde]
    if not voies or len(a) == 0:
        noeuds, a, b, attr = np.zeros((0, 2)), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 4), np.int64)
        tx = ty = np.zeros(0, np.int64)
    else:
        # Plus grande composante connexe du groupe (comme build_graph_velo.py)
        garde = garder_composantes(len(noeuds), a, b, np.zeros(len(noeuds), np.int64))
        a, b, attr = a[garde], b[garde], attr[garde]

    # Numérotation : tuile par tuile (ordre ty, tx), puis carré de 1 km, puis longitude ; à partir de base0
    utilises = np.unique(np.concatenate([a, b]))
    lon, lat = noeuds[utilises, 0], noeuds[utilises, 1]
    k_tuile = np.array([ordre_tuile[(x, y)] for x, y in zip(tx[utilises].tolist(), ty[utilises].tolist())], dtype=np.int64)
    carre_y = np.floor((LAT0 - lat) / CARRE_TRI_DEG[1]).astype(np.int64)
    carre_x = np.floor((lon - LON0) / CARRE_TRI_DEG[0]).astype(np.int64)
    tri = np.lexsort((lon, carre_x, carre_y, k_tuile))
    gid = np.empty(len(noeuds), np.int64)
    gid[utilises[tri]] = np.arange(len(utilises))
    a, b = gid[a], gid[b]                                   # indices locaux au groupe ; + base0 à l'écriture
    lon, lat, k_tuile = lon[tri], lat[tri], k_tuile[tri]
    longueur = np.hypot((lon[a] - lon[b]) * np.cos(np.radians(lat[a])), lat[a] - lat[b]) * M_PAR_DEG
    l_dm = np.maximum(1, np.round(longueur * 10)).astype(np.int64)
    classe, vit, sens = attr[:, 0], attr[:, 1], attr[:, 2]
    attr_code = classe | (sens << 3) | (vit << 5)
    print(f"  graphe : {len(lon)} nœuds, {len(a)} arêtes, {longueur.sum() / 1000:.0f} km")
    if controle is not None:
        controle.append((len(lon), a + base0, b + base0, l_dm, attr_code))

    x, y = vers_l93.transform(lon, lat)
    arbre_tout = cKDTree(np.column_stack([x, y])) if len(lon) else None
    amen = np.unique(np.concatenate([a[np.isin(classe, CLASSES_AMEN)], b[np.isin(classe, CLASSES_AMEN)]]))
    arbre_amen = cKDTree(np.column_stack([x[amen], y[amen]])) if len(amen) else None
    debut_tuile = np.searchsorted(k_tuile, np.arange(len(ordre_tuile) + 1))
    index, octets_total = [], 0
    ii, jj = np.meshgrid(np.arange(CELLULES_TUILE), np.arange(CELLULES_TUILE))
    for (tx0, ty0), k in sorted(ordre_tuile.items(), key=lambda kv: kv[1]):
        deb, fin = int(debut_tuile[k]), int(debut_tuile[k + 1])
        possedees = (a >= deb) & (a < fin)                   # arête rattachée à la tuile de son nœud a
        clon = (LON0 + (tx0 * CELLULES_TUILE + ii + 0.5) * DLON).ravel()
        clat = (LAT0 - (ty0 * CELLULES_TUILE + jj + 0.5) * DLAT).ravel()
        terre = masque.cellules(clon, clat)
        cx, cy = vers_l93.transform(clon, clat)
        aucun = (np.full(len(cx), np.inf), np.full(len(cx), -1))
        d_tout, p_tout = arbre_tout.query(np.column_stack([cx, cy])) if arbre_tout is not None else aucun
        if arbre_amen is not None:
            d_amen, p_amen = arbre_amen.query(np.column_stack([cx, cy]))
            p_amen = amen[p_amen]
        else:
            d_amen, p_amen = aucun
        # Cellules en mer : aucun nœud (allège la tuile ; tempsGrille les ignore de toute façon), sauf à moins
        # de MER_PRO_MAX_M d'un nœud : sinon un pont ou une jetée n'est le nœud d'aucune cellule et
        # accrocher() ne peut pas s'y raccrocher (curseur sur le pont d'Oléron accroché à la côte à 800 m)
        p_tout = np.where((terre | (d_tout <= MER_PRO_MAX_M)) & (p_tout >= 0), p_tout + base0, -1)
        p_amen = np.where((terre | (d_amen <= MER_PRO_MAX_M)) & (p_amen >= 0), p_amen + base0, -1)
        t = {
            "tx": tx0, "ty": ty0, "base": base0 + deb,
            "lon": np.round(lon[deb:fin] * 1e5).astype(np.int64).tolist(),
            "lat": np.round(lat[deb:fin] * 1e5).astype(np.int64).tolist(),
            "a": (a[possedees] - deb).tolist(), "b": (b[possedees] + base0).tolist(),
            "l": l_dm[possedees].tolist(), "attr": attr_code[possedees].tolist(),
            "ncx": CELLULES_TUILE, "ncy": CELLULES_TUILE, "terre": terre.astype(int).tolist(),
            "pro_tout": p_tout.tolist(), "pro_amen": p_amen.tolist(),
            "dist_tout": np.minimum(d_tout, 1e6).tolist(), "dist_amen": np.minimum(d_amen, 1e6).tolist(),
        }
        octets = encoder_tuile(t)
        (sortie / f"{tx0}_{ty0}.bin").write_bytes(octets)
        octets_total += len(octets)
        index.append([tx0, ty0, base0 + deb, fin - deb])
    return index, len(lon), octets_total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", action="append", required=True, help="GeoParquet produit par extraire.py (répétable, motif * accepté)")
    ap.add_argument("--emprise", action="append", default=[], help="lon_min,lat_min,lon_max,lat_max (répétable)")
    ap.add_argument("--departement", help="code du département (ex. 17) : emprise = contour OSM + marge, contour écrit dans la sortie")
    ap.add_argument("--marge-km", type=float, default=25, help="marge autour du département (défaut 25 km)")
    ap.add_argument("--chefs-lieux", help="chefs_lieux.json : ajoute une emprise de --rayon-km autour de chaque mairie")
    ap.add_argument("--rayon-km", type=float, default=8, help="demi-côté de l'emprise autour d'une mairie (défaut 8 km)")
    ap.add_argument("--sortie", default=str(ICI.parent / "marennes-oleron" / "tuiles"), help="dossier des tuiles (défaut : tuiles/ du site, dossier parent)")
    ap.add_argument("--reseau", help="réseau consolidé des collectivités (GeoJSON EPSG:2154, consolider_reseau.py) : "
                    "remplace les pistes OSM sur le territoire de --territoire")
    ap.add_argument("--territoire", default=str(ICI.parent / "cache" / "contour_territoire.geojson"),
                    help="communes du territoire des collectivités (GeoJSON WGS84 ; défaut : Marennes-Oléron)")
    ap.add_argument("--controle", help="écrit l'empreinte du graphe complet (test de recollage sans perte)")
    ap.add_argument("--duree-max-min", type=float, default=0,
                    help="s'arrête proprement après ce temps (0 = sans limite) ; relancer la même commande reprend")
    args = ap.parse_args()
    t0 = time.time()
    parquets = [str(p) for motif in args.parquet for p in (sorted(Path().glob(motif)) if "*" in motif else [Path(motif)])]
    emprises = [tuple(float(v) for v in e.split(",")) for e in args.emprise]
    con = connecter()
    sortie = Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    if args.departement:
        emprises.append(ecrire_contour_departement(con, parquets, args.departement, args.marge_km, sortie))
    if args.chefs_lieux:
        for c in json.loads(Path(args.chefs_lieux).read_text("utf-8")):
            dlat = args.rayon_km / 111.32
            dlon = args.rayon_km / (111.32 * math.cos(math.radians(c["lat"])))
            emprises.append((c["lon"] - dlon, c["lat"] - dlat, c["lon"] + dlon, c["lat"] + dlat))
    if not emprises:
        ap.error("--emprise, --departement ou --chefs-lieux requis")
    tuiles = tuiles_des_emprises(emprises)
    groupe = groupes_de_tuiles(tuiles)
    groupes = {}
    for t, g in groupe.items():
        groupes.setdefault(g, set()).add(t)
    print(f"{len(tuiles)} tuiles en {len(groupes)} groupe(s) de tuiles contiguës")
    vers_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
    lt, ht = DLON * CELLULES_TUILE, DLAT * CELLULES_TUILE
    boites = [(LON0 + tx * lt, LAT0 - (ty + 1) * ht, LON0 + (tx + 1) * lt, LAT0 - ty * ht) for tx, ty in tuiles]

    # Plusieurs fichiers ou plusieurs groupes : une seule lecture des sources vers un fichier intermédiaire
    # Reprise : la progression (groupes faits, index, numéro de nœud suivant) est enregistrée après chaque
    # groupe ; une relance avec les mêmes arguments repart du groupe suivant. Signature = arguments.
    signature = json.dumps([sorted(parquets), sorted(map(list, tuiles)), args.reseau])
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
    # Groupes traités l'un après l'autre (mémoire = un seul groupe) ; numérotation continue d'un groupe à l'autre
    for rang in range(prog["faits"], len(ordre)):
        if args.duree_max_min and time.time() - t0 > args.duree_max_min * 60:
            print(f"Arrêt après {args.duree_max_min:g} min : {rang}/{len(ordre)} groupes faits. Relancez la même commande pour continuer.")
            return
        tg = ordre[rang]
        print(f"Groupe {rang + 1}/{len(ordre)} : {len(tg)} tuiles")
        lignes, nb, octets = traiter_groupe(con, source, tg, prog["base"], masque, vers_l93, sortie, controle, coll)
        prog["index"] += lignes
        prog["base"] += nb
        prog["octets"] += octets
        prog["faits"] = rang + 1
        fichier_prog.write_text(json.dumps(prog), "utf-8")
    index, base, octets_total = prog["index"], prog["base"], prog["octets"]
    if args.controle:
        cat = lambda i: np.concatenate([c[i] for c in controle])
        ecrire_controle(Path(args.controle), sum(c[0] for c in controle), cat(1), cat(2), cat(3), cat(4))
    (sortie / "tuiles.json").write_text(json.dumps({
        "version": 1, "genere": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": [Path(p).name for p in parquets], "terre": TERRE_ZIP.name,
        "collectivites": bool(args.reseau),        # réseau CCIO + CCBM intégré sur Marennes-Oléron
        "grille": {"lon0": LON0, "lat0": LAT0, "dlon": DLON, "dlat": DLAT, "cellulesTuile": CELLULES_TUILE},
        "tuiles": index,
    }, ensure_ascii=False), "utf-8")
    fichier_prog.unlink()                                        # terminé : plus rien à reprendre
    if args.reseau:
        ecrire_territoires(gj, sortie, masque, con, source)
    print(f"Écrit : {len(index)} tuiles, {base} nœuds, {octets_total / 1024 ** 2:.1f} Mo, "
          f"{octets_total / 1024 / max(1, len(index)):.0f} Ko par tuile en moyenne, en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

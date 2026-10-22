#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_graph_velo.py
Prépare les données de la carte « À portée de vélo » pour Marennes-Oléron (île d'Oléron + bassin de Marennes).

Étapes :
  1. Territoire = union des communes de la CdC de l'Île d'Oléron et de la CC du Bassin de Marennes (geo.api.gouv.fr)
  2. Voies cyclables, routes et chemins depuis OpenStreetMap (Overpass)
  3. Qualification de chaque tronçon : classe, vitesse selon revêtement, sens de circulation
  4. Graphe topologique (nœuds OSM), découpé au contour, plus grande composante connexe
  5. Grille ~100 m : nœud le plus proche par mode + distance d'accès
  6. Export JS (window.PORTEE_VELO = {...}) : la page s'ouvre par double-clic, sans serveur

Classes de tronçons :
  0 piste      highway=cycleway, ou chemin avec bicycle=designated (voies vertes)
  1 bande      route avec cycleway*=lane|track|shared_busway, ou vélorue
  2 route      route ou chemin partagé (résidentielle, communale, track, path…)
  3 secondaire secondary(_link) sans aménagement
  4 principale primary(_link) sans aménagement (mode « tout » : autorisée, temps ×3 côté page)

Modes (les pénalités se règlent dans index.html, objet CFG) :
  « Pistes et bandes » : classes 0-1
  « Pistes + routes »  : classes 0-4 (principales ×3)

Usage (Python OSGeo4W : numpy, shapely >= 2, pyproj ; scipy facultatif) :
  python build_graph_velo.py
  python build_graph_velo.py --cache cache        # garde les téléchargements pour les relances
  python build_graph_velo.py --pas 100 --sortie data/portee_velo.js
"""

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
GEO_API = "https://geo.api.gouv.fr"
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "SIT-Marennes-Oleron/portee-velo-poc"

# Territoire = CdC de l'Île d'Oléron + CC du Bassin de Marennes (code EPCI → communes INSEE, pour le repli)
EPCI_TERRITOIRE = {
    "241700624": ("17093", "17140", "17323", "17337", "17385", "17411", "17485", "17486"),   # Île d'Oléron
    "241700699": ("17058", "17185", "17219", "17265", "17351", "17406"),                     # Bassin de Marennes
}
EPCI_CONTINENTAL = "241700699"      # seul EPCI du territoire avec des voisins terrestres (l'île n'a que le pont)

# --------------------------------------------------------------------------
# Paramètres du modèle
# --------------------------------------------------------------------------
PAS_GRILLE_M = 100
TAMPON_CONTOUR_M = 100            # garde les voies littorales un peu hors contour communal
CLASSES_TOUT = (0, 1, 2, 3, 4)       # doit suivre CFG.penalites.tout (index.html)
CLASSES_AMEN = (0, 1)
NOMS_CLASSES = ("piste", "bande", "route/chemin", "secondaire", "principale")
DEPART_DEFAUT = (-1.3063, 45.9443)  # centre de Saint-Pierre-d'Oléron (lon, lat)
MARGE_VOISINAGE = (0.25, 0.15)      # degrés lon/lat de terres voisines affichées autour du territoire
PONT_LONGUEUR_MIN_M = 1000          # ponts routiers longs = viaduc vers le continent
ZONE_TAMPON_M = 3_000               # le calcul déborde du territoire de 3 km sur le continent (accès, itinéraires de bord)
TAMPON_PONT_M = 60                  # le tablier passe au-dessus de la mer : on l'inclut explicitement
CLASSE_PONT = 3                     # viaduc sans aménagement : traité comme une route secondaire
APPROCHE_PONT_M = 500               # bouts de route principale aux têtes de pont, rattachés au pont
RACCORD_MAX_M = 25                  # extrémité libre accrochée à la voie la plus proche (> tampon de 20 m de osm_hors_cdc.py)
CONFONDU_M = 1.0                    # deux points à moins de 1 m = le même nœud
ORIGINES = ("osm", "ccio", "osm_valide", "raccord", "ccbm")   # origine de chaque arête (exportée dans aretes.o)

HIGHWAYS = (
    "cycleway", "path", "footway", "pedestrian", "track", "bridleway",
    "living_street", "residential", "unclassified", "road", "service",
    "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link",
)
CHEMINS = {"path", "footway", "pedestrian", "track", "bridleway"}
VELO_OK = {"yes", "designated", "permissive", "destination"}
VELO_INTERDIT = {"no", "dismount", "use_sidepath"}
SERVICES_EXCLUS = {"driveway", "parking_aisle", "drive-through", "emergency_access"}
AMENAGEMENTS_CHAUSSEE = {"lane", "track", "shared_busway"}
CLES_CYCLEWAY = ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right")

# Vitesses en km/h selon le revêtement OSM (tag surface)
# Base 20 km/h sur revêtement lisse (choix d'Aurélien, 08/10/2026) ; les autres revêtements gardent le même
# rapport qu'avec l'ancienne base de 15 km/h (×4/3, arrondi)
VITESSE_REVETEMENT = {
    "asphalt": 20, "paved": 20, "concrete": 20, "chipseal": 19,
    "concrete:plates": 19, "concrete:lanes": 19, "paving_stones": 17,
    "sett": 15, "cobblestone": 13, "unhewn_cobblestone": 11, "metal": 16, "wood": 16,
    "compacted": 17, "fine_gravel": 16,
    "unpaved": 13, "gravel": 13, "ground": 13, "dirt": 13, "earth": 13, "pebblestone": 12,
    "grass": 9, "woodchips": 9, "sand": 8, "mud": 7,
}
VITESSE_TRACKTYPE = {"grade1": 20, "grade2": 17, "grade3": 13, "grade4": 11, "grade5": 9}

REQUETE_OVERPASS = """[out:json][timeout:300];
(
  way["highway"~"^({hw})$"]({s},{w},{n},{e});
);
(._;>;);
out body;"""


# --------------------------------------------------------------------------
# Téléchargements
# --------------------------------------------------------------------------
def http_json(url, data=None, timeout=300, essais=3):
    """GET (ou POST si data) avec reprises, renvoie le JSON décodé."""
    derniere = None
    for i in range(essais):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as rep:
                return json.loads(rep.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - on veut réessayer quel que soit l'incident réseau
            derniere = exc
            attente = 5 * (i + 1)
            print(f"  ! échec ({exc}), nouvel essai dans {attente} s")
            time.sleep(attente)
    raise RuntimeError(f"Requête impossible : {url}\n{derniere}")


def telecharger_contour():
    """Communes des EPCI du territoire en GeoJSON (contours), repli sur les listes INSEE."""
    entites = []
    for code, insee in EPCI_TERRITOIRE.items():
        try:
            gj = http_json(f"{GEO_API}/communes?codeEpci={code}&format=geojson&geometry=contour")
            if not gj.get("features"):
                raise RuntimeError("aucune commune")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! EPCI {code} introuvable ({exc}), repli sur la liste INSEE")
            gj = http_json(f"{GEO_API}/communes?codeDepartement=17&format=geojson&geometry=contour")
            gj["features"] = [f for f in gj["features"] if f["properties"]["code"] in insee]
        if len(gj["features"]) != len(insee):
            print(f"  ! EPCI {code} : {len(gj['features'])} communes sur {len(insee)} attendues")
        entites += gj["features"]
    return {"type": "FeatureCollection", "features": entites}


def charger_contour(cache):
    chemin = cache / "contour_territoire.geojson" if cache else None
    if chemin and chemin.exists():
        print(f"Contour : cache {chemin}")
        gj = json.loads(chemin.read_text("utf-8"))
    else:
        print("Contour : geo.api.gouv.fr")
        gj = telecharger_contour()
        if chemin:
            chemin.write_text(json.dumps(gj), "utf-8")
    noms = sorted(f["properties"].get("nom", "?") for f in gj["features"])
    print(f"  {len(noms)} communes : {', '.join(noms)}")
    terr = unary_union([shape(f["geometry"]) for f in gj["features"]])
    # partie continentale du territoire : la bande de calcul ne s'étend qu'autour d'elle
    continental = unary_union([shape(f["geometry"]) for f in gj["features"]
                               if f["properties"].get("code") in EPCI_TERRITOIRE[EPCI_CONTINENTAL]])
    return terr, continental


def charger_osm(cache, bbox):
    chemin = cache / "osm_zone.json" if cache else None
    if chemin and chemin.exists():
        print(f"OSM : cache {chemin}")
        return json.loads(chemin.read_text("utf-8"))
    s, w, n, e = bbox
    requete = REQUETE_OVERPASS.format(hw="|".join(HIGHWAYS), s=s, w=w, n=n, e=e)
    donnees = urllib.parse.urlencode({"data": requete}).encode()
    for url in OVERPASS:
        print(f"OSM : {url}")
        try:
            osm = http_json(url, data=donnees, timeout=330, essais=2)
            break
        except RuntimeError as exc:
            print(f"  ! {exc}")
    else:
        raise SystemExit("Aucun serveur Overpass n'a répondu. Relancez plus tard.")
    if chemin:
        chemin.write_text(json.dumps(osm), "utf-8")
    return osm


def charger_voisinage(cache, terr_wgs, vers_l93, vers_wgs):
    """Terres voisines (communes de Charente-Maritime hors île), pour le fond de carte uniquement."""
    chemin = cache / "communes17.geojson" if cache else None
    if chemin and chemin.exists():
        gj = json.loads(chemin.read_text("utf-8"))
    else:
        print("Voisinage : geo.api.gouv.fr (communes 17)")
        gj = http_json(f"{GEO_API}/communes?codeDepartement=17&format=geojson&geometry=contour&fields=nom,code")
        if chemin:
            chemin.write_text(json.dumps(gj), "utf-8")
    w, s, e, n = terr_wgs.bounds
    cadre = shapely.box(w - MARGE_VOISINAGE[0], s - MARGE_VOISINAGE[1], e + MARGE_VOISINAGE[0], n + MARGE_VOISINAGE[1])
    ile_tampon = terr_wgs.buffer(0.001)
    geoms = [shape(f["geometry"]) for f in gj["features"]]
    geoms = [g for g in geoms if g.intersects(cadre) and not ile_tampon.contains(g.representative_point())]
    terre = reprojeter(unary_union(geoms).intersection(cadre), vers_l93).buffer(0).simplify(30)
    print(f"  {len(geoms)} communes voisines")
    return reprojeter(terre, vers_wgs)


def extraire_ponts(voies, coords):
    """Grands ponts routiers (le viaduc vers le continent), en lignes lon/lat."""
    ponts = []
    for v in voies:
        t = v.get("tags", {})
        if t.get("bridge") in (None, "no") or t.get("highway") not in ("primary", "trunk", "secondary", "cycleway"):
            continue
        pts = [coords[i] for i in v["nodes"] if i in coords]
        if len(pts) < 2:
            continue
        lon, lat = np.array(pts).T
        long_m = np.hypot(np.diff(lon) * 111_320 * math.cos(math.radians(lat.mean())), np.diff(lat) * 111_320).sum()
        if long_m >= PONT_LONGUEUR_MIN_M:
            ponts.append({"id": v["id"], "nom": t.get("name", ""), "coords": [[round(a, 5), round(b, 5)] for a, b in pts]})
            print(f"  pont : {t.get('name', '?')} ({long_m / 1000:.1f} km)")
    return ponts


def extraire_osm_cyclable(voies, coords, terr_l93, vers_l93, chemin, date_osm):
    """Écrit les aménagements OSM du territoire (classes 0-1) en GeoJSON, point de départ de la
    consolidation dans QGIS. Une entité par voie OSM entière (pas découpée en arêtes).

    nature = « dedie » (voie à part : piste, voie verte) ou « chaussee » (bande portée par
    une route : la retirer du graphe couperait la route, cf. design doc étape 3c).
    """
    ile = terr_l93.buffer(TAMPON_CONTOUR_M)
    shapely.prepare(ile)
    entites, km = [], [0.0, 0.0]
    for v in voies:
        t = v.get("tags", {})
        r = classer(t)
        if r is None or r[0] > 1:
            continue
        pts = [coords[i] for i in v["nodes"] if i in coords]
        if len(pts) < 2:
            continue
        ligne = shapely.LineString(pts)
        ligne_l93 = reprojeter(ligne, vers_l93)
        if not ile.intersects(ligne_l93):
            continue                                   # continent : hors périmètre de la consolidation
        classe, vit, sens = r
        km[classe] += ligne_l93.length / 1000
        entites.append({
            "type": "Feature",
            "geometry": arrondir(mapping(ligne), 6),
            "properties": {
                "osm_id": v["id"],
                "classe": classe,
                "nature": "chaussee" if classe == 1 and t.get("highway") not in CHEMINS | {"cycleway"} else "dedie",
                "highway": t.get("highway", ""),
                "nom": t.get("name") or t.get("ref") or "",
                "surface": t.get("surface", ""),
                "vitesse_kmh": vit,
                "sens": ("double", "unique", "inverse")[sens],   # « inverse » = sens opposé au tracé
                "cycleway": ";".join(f"{k}={t[k]}" for k in CLES_CYCLEWAY if k in t),
                "bicycle": t.get("bicycle", ""),
                "date_osm": date_osm,
                "url": f"https://www.openstreetmap.org/way/{v['id']}",
            },
        })
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps({"type": "FeatureCollection", "features": entites}, ensure_ascii=False), "utf-8")
    print(f"Extraction OSM : {len(entites)} voies, {km[0]:.1f} km de pistes, {km[1]:.1f} km de bandes → {chemin}")


def integrer_reseau(chemin, aretes, coords, nom_index, vers_l93, vers_wgs):
    """Ajoute le réseau cyclable consolidé (GeoJSON EPSG:2154, cf. consolider_reseau.py) au graphe
    et le raccorde aux voies existantes. Modifie aretes et coords en place.

    Arête = (nœud_a, nœud_b, classe, vitesse, sens, nom, origine, source). Les nœuds créés ont des ids
    négatifs (les ids OSM sont positifs).
      1. chaque ligne « existant » devient une chaîne d'arêtes ; sommets confondus au décimètre = même nœud
      2. croisement d'une arête consolidée avec une autre arête : nœud commun (les deux sont coupées)
      3. extrémité restée libre : reliée au point le plus proche d'une autre voie à moins de RACCORD_MAX_M
         (pas sur sa propre entité, sinon elle se raccrocherait à elle-même)
    Renvoie la liste des extrémités non raccordées [(x, y, source)] en Lambert 93.
    """
    # Coordonnées Lambert 93 des nœuds déjà dans le graphe
    ids_osm = sorted({n for ar in aretes for n in ar[:2]})
    X, Y = vers_l93.transform(np.array([coords[i][0] for i in ids_osm]), np.array([coords[i][1] for i in ids_osm]))
    xy = dict(zip(ids_osm, zip(X, Y)))
    compteur = [0]

    def nouveau(x, y):
        """Crée un nœud (id négatif) en Lambert 93 et en lon/lat."""
        compteur[0] -= 1
        xy[compteur[0]] = (x, y)
        coords[compteur[0]] = vers_wgs.transform(x, y)
        return compteur[0]

    # 1. Lignes consolidées → arêtes
    debut = len(aretes)
    sommets = {}
    for f in json.loads(Path(chemin).read_text("utf-8"))["features"]:
        p = f["properties"]
        if p.get("statut") != "existant" or not f["geometry"]:
            continue                                   # projeté : hors graphe v1
        g = shape(f["geometry"])
        origine = {"ccio": 1, "ccbm": 4}.get(p.get("origine"), 2)   # sinon : complément OSM
        sens = 1 if p.get("sens") == "unique" else 0
        source = f"{p.get('origine')}:{p.get('id_source')}"
        for ligne in getattr(g, "geoms", [g]):
            nds = []
            for c in ligne.coords:
                cle = (round(c[0], 1), round(c[1], 1))
                if cle not in sommets:
                    sommets[cle] = nouveau(c[0], c[1])
                nds.append(sommets[cle])
            for a, b in zip(nds, nds[1:]):
                if a != b:
                    aretes.append((a, b, int(p["classe"]), int(p["vitesse_kmh"]), sens,
                                   nom_index(p.get("nom") or ""), origine, source))
    fin = len(aretes)

    geoms = [shapely.LineString([xy[ar[0]], xy[ar[1]]]) for ar in aretes]
    arbre = shapely.STRtree(geoms)
    coupes = {}          # arête → nœuds où la couper
    connecteurs = {}     # (n1, n2) → arête consolidée dont le connecteur reprend les attributs
    raccordes = set()

    def extremite(k, px, py):
        """Extrémité de l'arête k confondue avec (px, py), sinon None."""
        for n in aretes[k][:2]:
            if math.hypot(xy[n][0] - px, xy[n][1] - py) < CONFONDU_M:
                return n
        return None

    # 2. Croisements
    nb_croisements = 0
    entree, cible = arbre.query(geoms[debut:fin], predicate="intersects")
    for i, k2 in zip(entree, cible):
        k1, k2 = debut + int(i), int(k2)
        if k1 == k2 or (debut <= k2 < k1) or set(aretes[k1][:2]) & set(aretes[k2][:2]):
            continue                                   # même arête, paire déjà vue, ou arêtes contiguës
        inter = geoms[k1].intersection(geoms[k2])
        for pt in getattr(inter, "geoms", [inter]):
            if pt.geom_type != "Point":
                continue                               # tronçons superposés (doublon OSM/CCIO) : rien à couper
            n1, n2 = extremite(k1, pt.x, pt.y), extremite(k2, pt.x, pt.y)
            if n1 is not None and n2 is not None:
                if n1 != n2:
                    connecteurs[(n1, n2)] = k1
            elif n1 is not None:
                coupes.setdefault(k2, set()).add(n1)
            elif n2 is not None:
                coupes.setdefault(k1, set()).add(n2)
            else:
                n = nouveau(pt.x, pt.y)
                coupes.setdefault(k1, set()).add(n)
                coupes.setdefault(k2, set()).add(n)
            raccordes.update(n for n in (n1, n2) if n is not None)
            nb_croisements += 1

    # 3. Extrémités libres (une seule arête consolidée, aucun croisement)
    degre, arete_de = {}, {}
    for k in range(debut, fin):
        for n in aretes[k][:2]:
            degre[n] = degre.get(n, 0) + 1
            arete_de[n] = k
    libres = [n for n, d in degre.items() if d == 1 and n not in raccordes]
    pts = shapely.points([xy[n] for n in libres])
    candidats = {}
    for i, k in zip(*arbre.query(pts, predicate="dwithin", distance=RACCORD_MAX_M)):
        candidats.setdefault(int(i), []).append(int(k))
    non_raccordes = []
    for i, n in enumerate(libres):
        k0 = arete_de[n]
        source = aretes[k0][7]
        valides = [k for k in candidats.get(i, []) if n not in aretes[k][:2] and aretes[k][7] != source]
        if not valides:
            non_raccordes.append((*xy[n], source))
            continue
        k = min(valides, key=lambda k: geoms[k].distance(pts[i]))
        proj = geoms[k].interpolate(geoms[k].project(pts[i]))
        m = extremite(k, proj.x, proj.y)
        if m is None:
            m = nouveau(proj.x, proj.y)
            coupes.setdefault(k, set()).add(m)
        connecteurs[(n, m)] = k0

    # Application des coupes : l'arête a→b devient a→n1→n2→…→b (nœuds triés le long du segment)
    nouvelles = []
    for k, ar in enumerate(aretes):
        if k not in coupes:
            nouvelles.append(ar)
            continue
        a, b = ar[:2]
        (ax, ay), (bx, by) = xy[a], xy[b]
        l2 = (bx - ax) ** 2 + (by - ay) ** 2 or 1.0
        milieu = sorted((((xy[n][0] - ax) * (bx - ax) + (xy[n][1] - ay) * (by - ay)) / l2, n)
                        for n in coupes[k] if n not in (a, b))
        chaine = [a] + [n for _, n in milieu] + [b]
        nouvelles += [(u, v) + ar[2:] for u, v in zip(chaine, chaine[1:]) if u != v]
    for (n1, n2), k0 in connecteurs.items():
        ar = aretes[k0]
        nouvelles.append((n1, n2, ar[2], ar[3], 0, ar[5], 3, ar[7]))   # connecteur : double sens
    aretes[:] = nouvelles

    print(f"Réseau consolidé : {fin - debut} arêtes, {nb_croisements} croisements, "
          f"{len(libres)} extrémités libres dont {len(libres) - len(non_raccordes)} raccordées "
          f"(≤ {RACCORD_MAX_M} m), {len(non_raccordes)} non raccordées")
    return non_raccordes


def ecrire_non_raccordes(chemin, points):
    """Points à contrôler dans QGIS (EPSG:2154) : [(x, y, type, source)]."""
    entites = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(x, 2), round(y, 2)]},
                "properties": {"type": t, "source": s}} for x, y, t, s in points]
    Path(chemin).write_text(json.dumps({
        "type": "FeatureCollection", "name": "non_raccordes",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}},
        "features": entites}, ensure_ascii=False), "utf-8")
    print(f"  {len(entites)} points à contrôler → {chemin}")


# --------------------------------------------------------------------------
# Qualification des tronçons
# --------------------------------------------------------------------------
def a_amenagement_chaussee(t):
    if t.get("bicycle_road") == "yes" or t.get("cyclestreet") == "yes":
        return True
    return any(t.get(k) in AMENAGEMENTS_CHAUSSEE for k in CLES_CYCLEWAY)


def vitesse_kmh(t, hw, classe):
    surf = t.get("surface")
    if surf in VITESSE_REVETEMENT:
        return VITESSE_REVETEMENT[surf]
    if hw == "track":
        return VITESSE_TRACKTYPE.get(t.get("tracktype"), 15)
    if hw in ("path", "bridleway"):
        return 17 if classe == 0 else 13   # voie verte souvent stabilisée
    if hw in ("footway", "pedestrian", "living_street"):
        return 16                            # cohabitation avec les piétons
    return 20


def sens_circulation(t):
    """0 = double sens, 1 = sens du tracé seulement, 2 = sens inverse seulement."""
    if t.get("oneway:bicycle") == "no":
        return 0
    if any(str(t.get(k, "")).startswith("opposite") for k in ("cycleway", "cycleway:left", "cycleway:right")):
        return 0
    ow = t.get("oneway:bicycle") or t.get("oneway")
    if ow is None and t.get("junction") in ("roundabout", "circular"):
        ow = "yes"
    if ow in ("yes", "1", "true"):
        return 1
    if ow == "-1":
        return 2
    return 0


def classer(t):
    """Renvoie (classe, vitesse_kmh, sens) ou None si le tronçon n'est pas cyclable."""
    hw = t.get("highway")
    velo = t.get("bicycle")
    if hw not in HIGHWAYS or velo in VELO_INTERDIT or t.get("area") == "yes":
        return None
    if t.get("access") in ("no", "private") and velo not in VELO_OK:
        return None
    if hw == "service" and t.get("service") in SERVICES_EXCLUS:
        return None
    if hw in ("footway", "pedestrian", "bridleway") and velo not in VELO_OK:
        return None

    if hw == "cycleway" or (hw in CHEMINS and velo == "designated"):
        classe = 0
    elif a_amenagement_chaussee(t):
        classe = 1
    elif hw in ("primary", "primary_link"):
        classe = 4
    elif hw in ("secondary", "secondary_link"):
        classe = 3
    else:
        classe = 2
    return classe, vitesse_kmh(t, hw, classe), sens_circulation(t)


# --------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------
def plus_proches(px, py, qx, qy):
    """Index et distance (m) du point (px, py) le plus proche de chaque requête (qx, qy)."""
    try:
        from scipy.spatial import cKDTree
        dist, idx = cKDTree(np.column_stack([px, py])).query(np.column_stack([qx, qy]))
        return idx.astype(np.int64), dist
    except ImportError:
        arbre = shapely.STRtree(shapely.points(px, py))
        paires = arbre.query_nearest(shapely.points(qx, qy), all_matches=False)
        idx = np.empty(len(qx), dtype=np.int64)
        idx[paires[0]] = paires[1]
        return idx, np.hypot(px[idx] - qx, py[idx] - qy)


def reprojeter(geom, transformer):
    """Reprojette une géométrie shapely avec un Transformer pyproj."""
    return shapely.transform(geom, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])))


def arrondir(obj, nd=5):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, (list, tuple)):
        return [arrondir(o, nd) for o in obj]
    if isinstance(obj, dict):
        return {k: arrondir(v, nd) for k, v in obj.items()}
    return obj


class UnionFind:
    def __init__(self, n):
        self.parent = np.arange(n)

    def trouver(self, x):
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def unir(self, a, b):
        ra, rb = self.trouver(a), self.trouver(b)
        if ra != rb:
            self.parent[ra] = rb


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------
def main():
    dossier = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", help="dossier de cache des téléchargements (contour + OSM)")
    ap.add_argument("--pas", type=float, default=PAS_GRILLE_M, help="pas de la grille en mètres (défaut 100)")
    ap.add_argument("--sortie", default=str(dossier / "data" / "portee_velo.js"), help="fichier JS produit")
    ap.add_argument("--variable", default="PORTEE_VELO", help="nom de la variable JS (window.<nom>) ; PORTEE_VELO_OSM pour la page de comparaison")
    ap.add_argument("--extraire-osm", nargs="?", const=str(dossier / "osm_cyclable.geojson"), metavar="GEOJSON",
                    help="écrit les pistes et bandes OSM du territoire en GeoJSON (pour QGIS) puis s'arrête")
    ap.add_argument("--reseau", metavar="GEOJSON",
                    help="réseau cyclable consolidé (reseau_cyclable.geojson) : remplace les pistes OSM du territoire")
    args = ap.parse_args()

    t0 = time.time()
    cache = Path(args.cache) if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    vers_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
    vers_wgs = Transformer.from_crs(2154, 4326, always_xy=True)

    # 1. Contour -------------------------------------------------------------
    terr_wgs, continental_wgs = charger_contour(cache)
    terr_l93 = reprojeter(terr_wgs, vers_l93)
    print(f"  surface : {terr_l93.area / 1e6:.1f} km²")
    continent = charger_voisinage(cache, terr_wgs, vers_l93, vers_wgs)

    # Zone d'étude = territoire + terres voisines à moins de ZONE_TAMPON_M (+ tablier du pont, plus bas)
    # bande autour du bassin de Marennes seulement : autour de l'île, elle traversait la mer (forêt de la Coubre)
    cote_l93 = reprojeter(continent, vers_l93).intersection(reprojeter(continental_wgs, vers_l93).buffer(ZONE_TAMPON_M))
    zone_l93 = unary_union([terr_l93, cote_l93])
    zone_wgs = reprojeter(zone_l93, vers_wgs)
    print(f"  bande de calcul hors territoire : {cote_l93.area / 1e6:.1f} km² sur {ZONE_TAMPON_M / 1000:.0f} km")

    # 2. OSM -----------------------------------------------------------------
    w, s, e, n = zone_wgs.bounds
    marge = 0.01
    osm = charger_osm(cache, (s - marge, w - marge, n + marge, e + marge))
    elements = osm.get("elements", [])
    coords = {el["id"]: (el["lon"], el["lat"]) for el in elements if el["type"] == "node"}
    voies = [el for el in elements if el["type"] == "way"]
    print(f"  {len(voies)} voies, {len(coords)} nœuds, base OSM du {osm.get('osm3s', {}).get('timestamp_osm_base', '?')}")
    if args.extraire_osm:
        date_osm = osm.get("osm3s", {}).get("timestamp_osm_base", "")
        extraire_osm_cyclable(voies, coords, terr_l93, vers_l93, Path(args.extraire_osm), date_osm)
        return
    ponts = extraire_ponts(voies, coords)
    ids_ponts = {p["id"] for p in ponts}
    lignes_ponts = [reprojeter(shapely.LineString(p["coords"]), vers_l93) for p in ponts]
    zone_l93_tampon = unary_union([zone_l93.buffer(TAMPON_CONTOUR_M)] + [l.buffer(TAMPON_PONT_M) for l in lignes_ponts])
    shapely.prepare(zone_l93_tampon)

    # 3. Tronçons ------------------------------------------------------------
    noms, index_noms = [""], {"": 0}

    def nom_index(nom):
        """Index du nom de voie dans la table noms (ajouté si nouveau)."""
        if nom not in index_noms:
            index_noms[nom] = len(noms)
            noms.append(nom)
        return index_noms[nom]

    # Avec --reseau, le réseau consolidé remplace les aménagements OSM sur le territoire (île + bassin de Marennes) :
    # voies dédiées (classe 0) retirées, routes portant une bande (classe 1) gardées comme simples routes
    # (retirer la classe 1 couperait la route elle-même). Continent et pont : OSM inchangé.
    dans_terr = set()
    if args.reseau:
        ids_c = np.fromiter(coords.keys(), dtype=np.int64, count=len(coords))
        lon_c, lat_c = np.array(list(coords.values())).T
        x_c, y_c = vers_l93.transform(lon_c, lat_c)
        dans_terr = set(ids_c[shapely.contains_xy(terr_l93.buffer(TAMPON_CONTOUR_M), x_c, y_c)].tolist())
    sans_amenagement = lambda t: {k: v for k, v in t.items() if k not in CLES_CYCLEWAY + ("bicycle_road", "cyclestreet")}
    retirees = 0

    aretes = []          # (id_a, id_b, classe, vitesse, sens, nom, origine, source)
    refs_aretes = []     # ref routière de chaque arête (pour suivre l'axe du pont)
    refs_ponts = {v.get("tags", {}).get("ref") for v in voies if v["id"] in ids_ponts} - {None}
    ignorees = 0
    for v in voies:
        t = v.get("tags", {})
        r = classer(t)
        if v["id"] in ids_ponts and r is not None:
            r = (min(r[0], CLASSE_PONT), r[1], r[2])   # le viaduc est autorisé aux vélos, sans aménagement
        if r is None:
            ignorees += 1
            continue
        classe, vit, sens = r
        remplacee = args.reseau and classe <= 1 and v["id"] not in ids_ponts
        if remplacee and classe == 1:
            r_route = classer(sans_amenagement(t))
            classe_route = r_route[0] if r_route else None
        idn = nom_index(t.get("name") or t.get("ref") or "")
        nds = v["nodes"]
        for a, b in zip(nds, nds[1:]):
            if a != b and a in coords and b in coords:
                c = classe
                if remplacee and a in dans_terr and b in dans_terr:
                    if classe == 0 or classe_route is None:
                        retirees += 1
                        continue
                    c = classe_route
                aretes.append((a, b, c, vit, sens, idn, 0, None))
                refs_aretes.append(t.get("ref"))
    print(f"  {len(voies) - ignorees} voies cyclables retenues, {ignorees} écartées")
    if args.reseau:
        print(f"  {retirees} arêtes OSM de pistes retirées sur le territoire (remplacées par le réseau consolidé)")

    # Le viaduc et ses approches (même ref, ex. D 26) débouchent sur des routes principales exclues.
    # On suit l'axe depuis les têtes de pont et on reclasse les APPROCHE_PONT_M premiers mètres
    # de route principale rencontrés, pour raccorder le pont au réseau des deux côtés.
    # (noms de variables distincts de w, s, e, n : l'emprise sert plus bas pour la grille)
    ext = {nd for v in voies if v["id"] in ids_ponts for nd in (v["nodes"][0], v["nodes"][-1])}
    adj = {}
    for k, (a, b, classe, *_r) in enumerate(aretes):
        if classe == 4 or refs_aretes[k] in refs_ponts:
            adj.setdefault(a, []).append((k, b)); adj.setdefault(b, []).append((k, a))
    def long_m(a, b):
        (x1, y1), (x2, y2) = coords[a], coords[b]
        return math.hypot((x2 - x1) * 111_320 * math.cos(math.radians(y1)), (y2 - y1) * 111_320)
    meilleur = {nd: 0.0 for nd in ext}
    pile, reclassees = [(nd, 0.0) for nd in ext], set()
    while pile:
        nd, dd = pile.pop()
        for k, voisin in adj.get(nd, []):
            principale = aretes[k][2] == 4
            dv = dd + (long_m(nd, voisin) if principale else 0.0)
            if dv > APPROCHE_PONT_M:
                continue
            if principale:
                reclassees.add(k)
            if dv < meilleur.get(voisin, math.inf):
                meilleur[voisin] = dv; pile.append((voisin, dv))
    for k in reclassees:
        aretes[k] = (aretes[k][0], aretes[k][1], CLASSE_PONT) + aretes[k][3:]
    print(f"  approches du pont : {len(reclassees)} segments de route principale rattachés")

    non_raccordes = []
    if args.reseau:
        non_raccordes = [(x_, y_, "extremite_libre", s) for x_, y_, s in
                         integrer_reseau(args.reseau, aretes, coords, nom_index, vers_l93, vers_wgs)]

    # 4. Graphe : découpe, composante principale ------------------------------
    ids = np.array(sorted({x for ar in aretes for x in ar[:2]}), dtype=np.int64)
    pos = {nid: i for i, nid in enumerate(ids)}
    lon = np.array([coords[i][0] for i in ids])
    lat = np.array([coords[i][1] for i in ids])
    x, y = vers_l93.transform(lon, lat)
    dedans = shapely.contains_xy(zone_l93_tampon, x, y)

    A = np.array([pos[ar[0]] for ar in aretes])
    B = np.array([pos[ar[1]] for ar in aretes])
    C = np.array([ar[2] for ar in aretes])
    V = np.array([ar[3] for ar in aretes])
    S = np.array([ar[4] for ar in aretes])
    N = np.array([ar[5] for ar in aretes])
    O = np.array([ar[6] for ar in aretes])
    garde = dedans[A] & dedans[B]

    uf = UnionFind(len(ids))
    for a, b in zip(A[garde], B[garde]):
        uf.unir(a, b)
    utilises = np.unique(np.concatenate([A[garde], B[garde]]))
    racines = np.array([uf.trouver(i) for i in utilises])
    valeurs, effectifs = np.unique(racines, return_counts=True)
    principale = valeurs[np.argmax(effectifs)]
    print(f"  {len(valeurs)} composantes connexes, la principale regroupe "
          f"{effectifs.max() / effectifs.sum():.1%} des nœuds")

    racine = np.array([uf.trouver(i) for i in range(len(ids))])
    garde &= (racine[A] == principale) & (racine[B] == principale)

    if args.reseau:
        # Morceaux du réseau consolidé coupés du reste (écartés du graphe) : un point par entité source
        isoles = np.flatnonzero((O > 0) & ~garde)
        km_isoles = np.hypot(x[A[isoles]] - x[B[isoles]], y[A[isoles]] - y[B[isoles]]).sum() / 1000
        vus = {}
        for k in isoles:
            vus.setdefault(aretes[k][7], (x[A[k]], y[A[k]]))
        non_raccordes += [(px, py, "isole", s) for s, (px, py) in vus.items()]
        print(f"  réseau consolidé isolé du graphe : {km_isoles:.1f} km ({len(vus)} entités)")
        ecrire_non_raccordes(Path(args.reseau).with_name("non_raccordes.geojson"), non_raccordes)

    A, B, C, V, S, N, O = (arr[garde] for arr in (A, B, C, V, S, N, O))

    # renumérotation compacte
    gardes = np.unique(np.concatenate([A, B]))
    renum = -np.ones(len(ids), dtype=np.int64)
    renum[gardes] = np.arange(len(gardes))
    A, B = renum[A], renum[B]
    lon, lat, x, y = lon[gardes], lat[gardes], x[gardes], y[gardes]
    longueur = np.hypot(x[A] - x[B], y[A] - y[B])

    print(f"Graphe : {len(gardes)} nœuds, {len(A)} arêtes")
    for c, nom_c in enumerate(NOMS_CLASSES):
        print(f"  {nom_c:<13}{longueur[C == c].sum() / 1000:8.1f} km")
    for o, nom_o in enumerate(ORIGINES):
        if (O == o).any():
            print(f"  origine {nom_o:<11}{longueur[O == o].sum() / 1000:8.1f} km")

    # 5. Grille --------------------------------------------------------------
    lat_c = (s + n) / 2
    dlat = args.pas / 111_320
    dlon = args.pas / (111_320 * math.cos(math.radians(lat_c)))
    gw, gn = w - 2 * dlon, n + 2 * dlat
    nx = math.ceil((e - w) / dlon) + 4
    ny = math.ceil((n - s) / dlat) + 4
    ii, jj = np.meshgrid(np.arange(nx), np.arange(ny))          # k = j * nx + i, j = 0 au nord
    clon = (gw + (ii + 0.5) * dlon).ravel()
    clat = (gn - (jj + 0.5) * dlat).ravel()
    dedans_g = shapely.contains_xy(zone_wgs, clon, clat)    # cellules calculées (île + continent)
    terr_g = shapely.contains_xy(terr_wgs, clon, clat)
    oleron_wgs = terr_wgs.difference(continental_wgs)          # île seule (version ?territoire=oleron de la page)
    oleron_g = shapely.contains_xy(oleron_wgs, clon, clat)         # cellules comptées dans la couverture
    cx, cy = vers_l93.transform(clon, clat)

    grille = {"w": round(gw, 6), "n": round(gn, 6), "dlon": dlon, "dlat": dlat, "nx": nx, "ny": ny,
              "dedans": dedans_g.astype(int).tolist(), "ile": terr_g.astype(int).tolist(),
              "oleron": oleron_g.astype(int).tolist()}
    for cle, classes in (("tout", CLASSES_TOUT), ("amen", CLASSES_AMEN)):
        masque = np.isin(C, classes)
        # On n'accroche la grille qu'aux nœuds de la plus grande composante du réseau de CE mode :
        # sinon une cellule peut tomber sur un bout de voie relié au reste uniquement par une route
        # principale (exclue), et apparaître « hors d'atteinte » alors qu'une voie praticable est à côté.
        uf_m = UnionFind(len(x))
        for a, b in zip(A[masque], B[masque]):
            uf_m.unir(a, b)
        noeuds_mode = np.unique(np.concatenate([A[masque], B[masque]]))
        rac = np.array([uf_m.trouver(i) for i in noeuds_mode])
        val, eff = np.unique(rac, return_counts=True)
        isoles = len(noeuds_mode) - eff.max()
        if cle == "tout":
            # Pas pour le mode aménagé : le réseau de pistes est fragmenté par nature (reliées par des routes),
            # garder sa seule plus grande composante viderait le territoire.
            noeuds_mode = noeuds_mode[rac == val[np.argmax(eff)]]
            print(f"  {cle} : {isoles} nœuds hors du réseau principal ({len(val) - 1} îlots), ignorés pour l'accès")
        idx, dist = plus_proches(x[noeuds_mode], y[noeuds_mode], cx, cy)
        grille[f"pro_{cle}"] = noeuds_mode[idx].tolist()
        grille[f"dist_{cle}"] = np.round(dist).astype(int).tolist()
        part = (dist[terr_g] <= (2000 if cle == "tout" else 800)).mean()
        print(f"  grille {cle} : {part:.1%} du territoire à portée d'accès du réseau")
    print(f"Grille : {nx} × {ny} cellules, {terr_g.sum()} sur le territoire, {dedans_g.sum() - terr_g.sum()} dans la bande de {ZONE_TAMPON_M // 1000} km")

    # 6. Export --------------------------------------------------------------
    contour = reprojeter(terr_l93.simplify(15), vers_wgs)
    depart = DEPART_DEFAUT
    if not terr_wgs.contains(Point(depart)):
        rp = terr_wgs.representative_point()
        depart = (rp.x, rp.y)

    donnees = {
        "meta": {
            "genere": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "osm": osm.get("osm3s", {}).get("timestamp_osm_base", ""),
            "pas_m": args.pas,
            "depart_defaut": [round(depart[0], 5), round(depart[1], 5)],
            "km_par_classe": [round(float(longueur[C == c].sum()) / 1000, 1) for c in range(5)],
            "origines": ORIGINES,
            "reseau": Path(args.reseau).name if args.reseau else "osm",
        },
        "contour": arrondir(mapping(contour)),
        "contour_oleron": arrondir(mapping(reprojeter(reprojeter(oleron_wgs, vers_l93).simplify(15), vers_wgs))),
        "continent": arrondir(mapping(continent), 4),
        "limite_continent": arrondir(mapping(reprojeter(cote_l93.simplify(15), vers_wgs))),
        "ponts": ponts,
        "noeuds": {
            "lon": np.round(lon * 1e5).astype(int).tolist(),
            "lat": np.round(lat * 1e5).astype(int).tolist(),
        },
        "aretes": {
            "a": A.tolist(),
            "b": B.tolist(),
            "l": np.maximum(1, np.round(longueur * 10)).astype(int).tolist(),   # décimètres
            "attr": (C | (S << 3) | (V << 5)).astype(int).tolist(),            # classe | sens | vitesse
            "nom": N.tolist(),
            "o": O.tolist(),                                                    # index dans meta.origines
        },
        "noms": noms,
        "grille": grille,
    }
    sortie = Path(args.sortie)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    texte = f"window.{args.variable}=" + json.dumps(donnees, separators=(",", ":"), ensure_ascii=False) + ";\n"
    sortie.write_text(texte, "utf-8")
    print(f"Écrit : {sortie} ({len(texte.encode('utf-8')) / 1e6:.1f} Mo) en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

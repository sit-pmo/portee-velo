#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
consolider_reseau.py
Fusionne les réseaux cyclables des collectivités (CCIO, CCBM) et les compléments OSM validés en une
seule couche au format attendu par build_graph_velo.py (--reseau).

Entrées (GeoJSON, EPSG:2154) :
  ccio_cyclable.geojson    export de la base CCIO (codes pcy_*), fait foi là où il existe
  ccbm_cyclable.geojson    export de la base CCBM (mêmes codes pcy_*), fait foi là où il existe
  osm_hors_ccio_ok.geojson morceaux OSM hors couverture CCIO, vérifiés dans QGIS (tout ce qui reste = validé)
  osm_hors_ccbm_ok.geojson idem pour la CCBM ; à défaut, osm_hors_ccbm.geojson (non vérifié, provisoire)

Sortie : reseau_cyclable.geojson (EPSG:2154), une LineString par entité, champs :
  classe      0 piste / voie verte, 1 bande, 2 route partagée (itinéraire sans aménagement)
  statut      existant / projete (projete exclu du graphe)
  sens        double / unique (unique = sens de numérisation)
  vitesse_kmh selon le revêtement
  origine     ccio / ccbm / osm
  nom, id_source

Usage : python consolider_reseau.py   (fichiers lus et écrits à la racine du dépôt)
"""

import json
from pathlib import Path

from shapely.geometry import mapping, shape

from build_graph_velo import vitesse_kmh as vitesse_osm   # même table de vitesses OSM que le graphe

# --------------------------------------------------------------------------
# Correspondances CCIO (validées avec Aurélien le 2026-10-08)
# --------------------------------------------------------------------------
# pcy_typame_id → classe. 14 « Autre » = traversées de chaussée (11 m en moyenne) : pistes,
# sinon le réseau serait coupé à chaque carrefour. 15 « Aucun » = itinéraire jalonné sans aménagement.
CLASSE_TYPAME = {
    1: 0, 3: 0, 6: 0, 9: 0, 10: 0, 11: 0, 14: 0,
    2: 1, 4: 1, 7: 1, 8: 1, 12: 1, 13: 1,
    5: 2, 15: 2,
}
# pcy_etarea_id : 2 Réalisée, 5 Provisoire = existant ; 1 En cours, 3 Étude, 4 Projet = projeté.
# Vide (26 tronçons sur la piste de Ponthézière) : compté existant par défaut.
STATUT_ETAREA = {2: "existant", 5: "existant", 1: "projete", 3: "projete", 4: "projete", None: "existant"}
# pcy_sens_id : 1 Unidirectionnel, 2 Bidirectionnel (vide = double)
SENS_ID = {1: "unique", 2: "double", None: "double"}
# pcy_revet_id → vitesse. Catégorie 1 (lisse : béton, enrobé, grave émulsion) 15 km/h,
# catégorie 2 (bicouche, stabilisé, bois) 12 km/h. Inconnu : 15.
VITESSE_REVET = {1: 20, 5: 20, 6: 20, 7: 20, 2: 16, 3: 16, 4: 16, 8: 16, 9: 16}   # base 20 km/h (×4/3 de l'ancienne base 15)
VITESSE_DEFAUT = 20
# CCBM : pcy_etarea_id vaut 4 (« Projet ») sur 121 tronçons sur 134, alors que etat_piste (Bon, Neuf…) et
# pcy_date_creation (2005-2024) montrent des pistes existantes : défaut de saisie, on force « existant ».
STATUT_FORCE = {"ccbm": "existant"}

CRS_2154 = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}}


def lignes(geom):
    """Éclate une (Multi)LineString en LineStrings."""
    return list(getattr(geom, "geoms", [geom]))


def classe_ccio(p):
    """Meilleur aménagement des deux côtés (classe la plus basse). Aucun code : route partagée."""
    classes = [CLASSE_TYPAME[c] for c in (p.get("pcy_typame_id_d"), p.get("pcy_typame_id_g")) if c in CLASSE_TYPAME]
    return min(classes, default=2)


def entite(geom, **props):
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


def main():
    dossier = Path(__file__).resolve().parents[2]   # couches des collectivités (non publiées) à la racine du dépôt
    sortie = []

    # Collectivités : font foi (même nomenclature pcy_* pour la CCIO et la CCBM)
    for origine in ("ccio", "ccbm"):
        for f in json.loads((dossier / f"{origine}_cyclable.geojson").read_text("utf-8"))["features"]:
            if not f["geometry"]:
                continue
            p = f["properties"]
            for l in lignes(shape(f["geometry"])):
                sortie.append(entite(
                    l,
                    classe=classe_ccio(p),
                    statut=STATUT_FORCE.get(origine) or STATUT_ETAREA.get(p.get("pcy_etarea_id"), "existant"),
                    sens=SENS_ID.get(p.get("pcy_sens_id"), "double"),
                    vitesse_kmh=VITESSE_REVET.get(p.get("pcy_revet_id"), VITESSE_DEFAUT),
                    origine=origine, nom=p.get("pcy_nom") or "", id_source=str(p.get("pcy_id")),
                ))

    # OSM : complète hors couverture des collectivités (fichiers vérifiés dans QGIS ; CCBM provisoire si absent)
    complements = [dossier / "osm_hors_ccio_ok.geojson"]
    ccbm_ok = dossier / "osm_hors_ccbm_ok.geojson"
    complements.append(ccbm_ok if ccbm_ok.exists() else dossier / "osm_hors_ccbm.geojson")
    if not ccbm_ok.exists():
        print("  ! osm_hors_ccbm_ok.geojson absent : compléments OSM du bassin de Marennes NON vérifiés (provisoire)")
    osm = [f for c in complements for f in json.loads(c.read_text("utf-8"))["features"]]
    for f in osm:
        if not f["geometry"]:
            continue
        p = f["properties"]
        for l in lignes(shape(f["geometry"])):
            sens = p.get("sens", "double")
            if sens == "inverse":                 # sens opposé au tracé : on retourne la ligne
                l, sens = l.reverse(), "unique"
            sortie.append(entite(
                l,
                classe=int(p["classe"]), statut="existant", sens=sens,
                vitesse_kmh=vitesse_osm({"surface": p.get("surface")}, p.get("highway"), int(p["classe"])),   # table à jour
                origine="osm", nom=p.get("nom") or "", id_source=str(p.get("osm_id")),
            ))

    chemin = dossier / "reseau_cyclable.geojson"
    chemin.write_text(json.dumps({"type": "FeatureCollection", "name": "reseau_cyclable", "crs": CRS_2154,
                                  "features": sortie}, ensure_ascii=False), "utf-8")

    # Bilan (sert aussi de contrôle : comparer avec les km attendus)
    bilan = {}
    for f in sortie:
        p = f["properties"]
        cle = (p["origine"], p["statut"], p["classe"])
        bilan[cle] = bilan.get(cle, 0) + shape(f["geometry"]).length / 1000
    print(f"Écrit : {chemin} ({len(sortie)} lignes)")
    for (orig, statut, classe), km in sorted(bilan.items()):
        print(f"  {orig:<5} {statut:<9} classe {classe} : {km:6.1f} km")


if __name__ == "__main__":
    main()

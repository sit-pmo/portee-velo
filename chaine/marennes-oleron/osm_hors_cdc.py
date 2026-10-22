#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osm_hors_cdc.py
Morceaux de pistes et bandes OSM situés dans les communes d'un EPCI mais hors de la couche cyclable
de la collectivité (tampon TAMPON_M) : ce sont les aménagements à vérifier dans QGIS avant de les
intégrer au réseau consolidé (tout ce qui reste dans le fichier vérifié = validé).

Usage : python osm_hors_cdc.py <couche_cdc.geojson> <code_epci> <sortie.geojson>
  ex.   python osm_hors_cdc.py ccbm_cyclable.geojson 241700699 osm_hors_ccbm.geojson
Prérequis : osm_cyclable.geojson (build_graph_velo.py --extraire-osm) et cache/contour_territoire.geojson.
"""

import json
import sys
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape

from build_graph_velo import EPCI_TERRITOIRE

TAMPON_M = 20          # écart de tracé toléré entre OSM et la couche de la collectivité
LONG_MIN_M = 50        # en dessous : effets de bord aux extrémités, sans intérêt
ID_PONT = 5013034      # pont de l'Île d'Oléron, traité à part par build_graph_velo.py


def main():
    couche, epci, sortie = sys.argv[1:4]
    dossier = Path(__file__).resolve().parents[2]   # cache/ local à la racine du dépôt
    vers_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
    l93 = lambda g: shapely.transform(g, lambda xy: np.column_stack(vers_l93.transform(xy[:, 0], xy[:, 1])))

    communes = json.loads((dossier / "cache" / "contour_territoire.geojson").read_text("utf-8"))["features"]
    zone = l93(shapely.union_all([shape(f["geometry"]) for f in communes
                                  if f["properties"]["code"] in EPCI_TERRITOIRE[epci]]))
    cdc = shapely.union_all([shape(f["geometry"]) for f in json.loads(Path(couche).read_text("utf-8"))["features"]
                             if f["geometry"]]).buffer(TAMPON_M)

    entites, km = [], {}
    for f in json.loads((dossier / "osm_cyclable.geojson").read_text("utf-8"))["features"]:
        p = f["properties"]
        if p["osm_id"] == ID_PONT:
            continue
        reste = l93(shape(f["geometry"])).intersection(zone).difference(cdc)
        for g in getattr(reste, "geoms", [reste]):
            if g.geom_type == "LineString" and g.length >= LONG_MIN_M:
                km[p["nature"]] = km.get(p["nature"], 0) + g.length / 1000
                entites.append({"type": "Feature", "geometry": mapping(shapely.set_precision(g, 0.01)),
                                "properties": {**p, "longueur_m": round(g.length), "decision": ""}})
    Path(sortie).write_text(json.dumps({
        "type": "FeatureCollection", "name": Path(sortie).stem,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}},
        "features": entites}, ensure_ascii=False), "utf-8")
    print(f"{sortie} : {len(entites)} morceaux, " + ", ".join(f"{k} {v:.1f} km" for k, v in km.items()))


if __name__ == "__main__":
    main()

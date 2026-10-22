"""Contour de la France métropolitaine pour la page (20/10/2026) : chaine/contour_fr.json.

La page s'en sert pour dessiner la limite et accepter un départ (dansZone : clic, survol, adresse, géolocalisation).
L'ancien contour (union de departements.json, simplifiée à ~500 m deux fois) coupait les caps et les ports :
1 232 km² de terre dehors, 23 mairies refusées (Antibes, Roscoff, Le Conquet, Perros-Guirec côté Ploumanac'h…).
Celui-ci part des contours des communes (geo.api.gouv.fr, Admin Express de l'IGN), élargis de 300 m puis
simplifiés à ~200 m : plus aucune terre ni mairie dehors, 0,1 Mo. La marge déborde un peu sur la mer et chez les
voisins, sans effet : les tuiles couvrent 2 km au-delà des départements.

tuiler.py --contour (ecrire_contour_zone) reprend ce fichier s'il existe ; deployer.yml et apercu.yml l'écrivent
dans tuiles/fr/contour.json, sans re-tuilage.

Usage (depuis chaine/, contours téléchargés comme dans scores_communes.yml) :
  python -I -X utf8 contour_metropole.py --communes 'cache/geo/contours/communes_*.geojson' --sortie contour_fr.json
"""
import argparse
import glob
import json
import sys
from pathlib import Path

import shapely
from pyproj import Transformer

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI)]   # -I retire le dossier du script du chemin
from lieux import nettoyer_contour  # noqa: E402

TAMPON_DEG = 0.004      # ~300 m (en latitude ; ~200 à 300 m en longitude) : garde les caps et les jetées
TOLERANCE_DEG = 0.002   # ~200 m, appliquée après le tampon : le contour reste au large de la côte
PRECISION = 4


def contour(geoms):
    union = shapely.union_all(shapely.make_valid(geoms))
    g = shapely.simplify(union.buffer(TAMPON_DEG), TOLERANCE_DEG)
    g = nettoyer_contour(g, Transformer.from_crs(4326, 2154, always_xy=True))
    return shapely.set_precision(g, 10 ** -PRECISION)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--communes", action="append", required=True, help="GeoJSON des communes (motif * accepté)")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()
    geoms = []
    for m in a.communes:
        for f in sorted(glob.glob(m)):
            if Path(f).stem.split("_", 1)[-1][:2] in ("97", "98"):
                continue                                  # métropole seule : les DROM ont leur propre contour
            geoms += [shapely.geometry.shape(ft["geometry"]) for ft in json.loads(Path(f).read_text("utf-8"))["features"]
                      if ft.get("geometry")]
    g = contour(geoms)
    Path(a.sortie).write_text(shapely.to_geojson(g), "utf-8")
    print(f"→ {a.sortie} : {len(geoms)} communes, {len(getattr(g, 'geoms', [g]))} parties, "
          f"{shapely.get_num_coordinates(g)} sommets, {Path(a.sortie).stat().st_size / 1e6:.2f} Mo")


if __name__ == "__main__":
    main()

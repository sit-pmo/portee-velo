"""Contours des communes par département, pour les aplats du classement (classement.html, 17/10/2026).

Source : geo.api.gouv.fr (contours Admin Express de l'IGN), un GeoJSON par département téléchargé par le workflow
scores_communes.yml. Tous les contours sont simplifiés ensemble (shapely.coverage_simplify : les limites
communes restent communes, ni trou ni chevauchement entre voisines), coordonnées à 4 décimales (~10 m).

Sortie, dans --sortie : <dep>.json (GeoJSON, propriété c = code INSEE, joint aux scores de communes.json par la page)
et index.json ({dep: [ouest, sud, est, nord]}) pour ne charger que les départements visibles ; france.json : toutes
les communes en une fois, plus simplifiées (~400 m, 3 décimales, ~2 Mo compressé), pour la vue d'ensemble.

Usage (depuis chaine/) :
  python -I -X utf8 contours_communes.py --communes 'cache/geo/communes_*.geojson' --sortie /tmp/scores/contours
"""
import argparse
import glob
import json
from pathlib import Path

import shapely

TOLERANCE_DEG = 0.0006         # ~50 m : assez fin pour les petites communes, léger (≈ 100 à 400 Ko par département)
PRECISION = 4
TOLERANCE_FRANCE_DEG = 0.005   # vue d'ensemble (classement.html sous le zoom 9) : 35 000 communes lisibles et légères
PRECISION_FRANCE = 3


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--communes", action="append", required=True, help="GeoJSON des communes (motif * accepté)")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()
    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    # Toutes les communes simplifiées d'un seul tenant : les limites entre départements restent communes elles aussi
    # (simplifiés séparément, deux départements voisins laissaient de fins liserés à leur frontière)
    deps, feats = [], []
    for m in a.communes:
        for f in map(Path, sorted(glob.glob(m))):
            for ft in json.loads(f.read_text("utf-8")).get("features", []):
                if ft.get("geometry"):
                    deps.append(f.stem.split("_", 1)[1])           # communes_<dep>.geojson
                    feats.append(ft)
    geoms = [shapely.geometry.shape(ft["geometry"]) for ft in feats]
    simples = shapely.set_precision(shapely.coverage_simplify(geoms, TOLERANCE_DEG), 10 ** -PRECISION)
    index, total = {}, 0
    for dep in sorted(set(deps)):
        sel = [i for i, d in enumerate(deps) if d == dep and not simples[i].is_empty]
        sortie_dep = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"c": feats[i]["properties"]["code"]}, "geometry": json.loads(shapely.to_geojson(simples[i]))}
            for i in sel]}
        (sortie / f"{dep}.json").write_text(json.dumps(sortie_dep, separators=(",", ":")), "utf-8")
        w, s_, e, n = shapely.total_bounds(simples[sel])
        index[dep] = [round(w, 3), round(s_, 3), round(e, 3), round(n, 3)]
        total += (sortie / f"{dep}.json").stat().st_size
    # Vue d'ensemble : toutes les communes, plus simplifiées, en un fichier
    grossiers = shapely.set_precision(shapely.coverage_simplify(geoms, TOLERANCE_FRANCE_DEG), 10 ** -PRECISION_FRANCE)
    (sortie / "france.json").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"c": ft["properties"]["code"]}, "geometry": json.loads(shapely.to_geojson(g))}
        for ft, g in zip(feats, grossiers) if not g.is_empty]}, separators=(",", ":")), "utf-8")
    (sortie / "index.json").write_text(json.dumps(index, separators=(",", ":")), "utf-8")
    print(f"→ {sortie} : {len(index)} départements, {total / 1e6:.1f} Mo")


if __name__ == "__main__":
    main()

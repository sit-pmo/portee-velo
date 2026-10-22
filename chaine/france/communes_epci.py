"""Communes et EPCI d'un territoire, pour les diagnostics par EPCI (phase 6, 17/10/2026).

Source : geo.api.gouv.fr (Etalab : COG de l'Insee, contours IGN Admin Express), téléchargé par le workflow
diagnostiquer.yml : communes par département en GeoJSON (code, nom, codeEpci, contour) et liste des EPCI (code, nom).

Sorties :
  <sortie>/epci.json        contours simplifiés des EPCI du territoire (union de leurs communes), pour la page
                            (EPCI du départ, onglet Services) : {"epci": [{code, nom, geometrie}]}
  <sortie>/communes.json    [[code INSEE, nom, code EPCI ou ""]] dans l'ordre des indices
  <sortie>/cellules.bin     cellules habitées (couche population de acces/, comptages_blocs.py) et leur commune :
                            n × (k : float64 indice global de cellule, c : int32 indice de commune, -1 si aucune),
                            triées par k ; lu par diagnostics.js

Usage (depuis chaine/, après comptages_blocs.py) :
  python -I -X utf8 communes_epci.py --tuiles ../tuiles/fr --communes 'cache/geo/communes_*.geojson' \\
      --epci cache/geo/epcis.json --sortie cache/diag/fr
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import shapely
from shapely.ops import unary_union

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI), str(ICI.parent)]
from comptages import Grille            # noqa: E402

SIMPLIFIER_DEG = 0.0015                   # ~150 m : contours d'EPCI légers pour la page (quelques Mo en métropole)
PRECISION = 4                             # décimales des coordonnées écrites (~10 m)
PROCHE_DEG = 0.004                        # cellule hors de tout contour (trait de côte) : commune à ~300 m au plus


def lire_communes(motifs, code_zone):
    """[(code, nom, epci, géométrie)] des communes du territoire (métropole : hors 97x ; DROM : 97x du code)."""
    out = {}
    for m in motifs:
        for f in (sorted(Path().glob(m)) if "*" in m else [Path(m)]):
            for ft in json.loads(f.read_text("utf-8")).get("features", []):
                p, g = ft["properties"], ft.get("geometry")
                code = p.get("code", "")
                dedans = code.startswith(code_zone) if code_zone != "fr" else not code.startswith("97")
                if g and dedans:
                    out[code] = (code, p.get("nom", code), p.get("codeEpci") or "", shapely.geometry.shape(g))
    return [out[c] for c in sorted(out)]


def cellules_habitees(dossier):
    """Indices globaux k des cellules de la couche population des blocs acces/ (comptages_blocs.py)."""
    ks = []
    for f in sorted((dossier / "acces").glob("*_*.json")):
        for c in json.loads(f.read_text("utf-8")).get("couches", []):
            if c["id"] == "population":
                ks.append(np.cumsum(np.asarray(c["k"], np.int64)))
    return np.unique(np.concatenate(ks)) if ks else np.zeros(0, np.int64)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tuiles", required=True)
    ap.add_argument("--communes", action="append", required=True, help="GeoJSON des communes (motif * accepté)")
    ap.add_argument("--epci", required=True, help="liste des EPCI (code, nom)")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()
    t0 = time.time()
    dossier, sortie = Path(a.tuiles), Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    grille = Grille(dossier)
    code_zone = json.loads((dossier / "tuiles.json").read_text("utf-8"))["grille"]["code"]
    noms_epci = {e["code"]: e["nom"] for e in json.loads(Path(a.epci).read_text("utf-8"))}
    communes = lire_communes(a.communes, code_zone)
    print(f"{len(communes)} communes, {len({c[2] for c in communes if c[2]})} EPCI")
    (sortie / "communes.json").write_text(json.dumps([c[:3] for c in communes], ensure_ascii=False,
                                                     separators=(",", ":")), "utf-8")

    # Commune de chaque cellule habitée : centre de la cellule dans le contour (STRtree, requête vectorisée), sinon
    # la plus proche (cellule de bord de mer dont le centre sort du contour)
    k = cellules_habitees(dossier)
    gj, gi = np.divmod(k, grille.W)
    pts = shapely.points(grille.lon0 + (gi + .5) * grille.dlon, grille.lat0 - (gj + .5) * grille.dlat)
    arbre = shapely.STRtree([c[3] for c in communes])
    ip, ic = arbre.query(pts, predicate="within")
    commune = np.full(len(k), -1, np.int32)
    commune[ip] = ic
    seules = np.flatnonzero(commune < 0)
    if len(seules):
        jp, jc = arbre.query_nearest(pts[seules], max_distance=PROCHE_DEG, all_matches=False)
        commune[seules[jp]] = jc
    print(f"{len(k):,} cellules habitées, {np.count_nonzero(commune < 0):,} sans commune")
    rec = np.zeros(len(k), dtype=[("k", "<f8"), ("c", "<i4")])
    rec["k"], rec["c"] = k, commune
    rec.tofile(sortie / "cellules.bin")

    # Contours des EPCI : union de leurs communes, simplifiée
    par_epci = {}
    for _code, _nom, e, g in communes:
        if e:
            par_epci.setdefault(e, []).append(g)
    liste = []
    for e, gs in sorted(par_epci.items()):
        g = shapely.set_precision(unary_union(gs).simplify(SIMPLIFIER_DEG, preserve_topology=True), 10 ** -PRECISION)
        if not g.is_empty:
            liste.append({"code": e, "nom": noms_epci.get(e, e), "geometrie": json.loads(shapely.to_geojson(g))})
    (sortie / "epci.json").write_text(json.dumps({"epci": liste}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"→ {sortie} : {len(liste)} EPCI, epci.json {(sortie / 'epci.json').stat().st_size / 1e6:.1f} Mo, "
          f"en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

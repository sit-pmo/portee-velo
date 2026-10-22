"""Contours simplifiés des EPCI de Charente-Maritime pour la page (tuiles/epci.json).

La page s'en sert pour trouver l'EPCI du départ D (point dans polygone) et charger son bilan
« territoire de la demi-heure » (tuiles/diagnostic_epci_<siren>.json, diagnostic_epci.js).

  python epci_contours.py <communes17.geojson> epci17.json <site>/tuiles/epci.json

communes17.geojson : contours des communes (code, nom), par ex.
  https://geo.api.gouv.fr/communes?codeDepartement=17&format=geojson&geometry=contour&fields=code,nom
epci17.json : composition des EPCI (code SIREN, nom, communes), tirée de @etalab/decoupage-administratif.
"""
import json, sys
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

TOLERANCE = 0.0008   # degrés (~60-90 m) : assez fin pour savoir dans quel EPCI tombe un clic

def arrondir(g, n=4):
    if isinstance(g, (list, tuple)):
        if g and isinstance(g[0], (int, float)):
            return [round(g[0], n), round(g[1], n)]
        return [arrondir(x, n) for x in g]
    return g

def main(f_communes, f_epci, f_sortie):
    communes = {f["properties"]["code"]: shape(f["geometry"]) for f in json.load(open(f_communes, encoding="utf-8"))["features"]}
    E = json.load(open(f_epci, encoding="utf-8"))
    sortie = []
    for e in E["epci"]:
        geoms = [communes[c] for c in e["communes"] if c in communes]
        manque = [c for c in e["communes"] if c not in communes]
        if manque:
            print(f"  {e['nom']} : {len(manque)} communes sans contour ({', '.join(manque[:5])})")
        g = unary_union(geoms).buffer(0).simplify(TOLERANCE, preserve_topology=True)
        sortie.append({"code": e["code"], "nom": e["nom"].replace('"', ""), "communes": len(e["communes"]),
                       "geometrie": {"type": g.geom_type, "coordinates": arrondir(mapping(g)["coordinates"])}})
        print(f"{e['code']} {e['nom']} : {len(geoms)} communes")
    txt = json.dumps({"version": 1, "source": E["source"], "epci": sortie}, ensure_ascii=False, separators=(",", ":"))
    open(f_sortie, "w", encoding="utf-8").write(txt)
    print(f"→ {f_sortie} ({len(txt) / 1e3:.0f} ko)")

if __name__ == "__main__":
    main(*sys.argv[1:4])

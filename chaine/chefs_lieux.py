"""Mairies des chefs-lieux des 96 départements métropolitains → chefs_lieux.json (points de départ des scores).

Chef-lieu : Code officiel géographique de l'INSEE (cache/v_departement.csv, colonne CHEFLIEU, millésime 2025).
Mairie : objet OSM amenity=townhall situé dans la commune (boundary=administrative, admin_level=8,
ref:INSEE = code du chef-lieu). Choix, dans l'ordre : townhall:type=city, puis nom contenant « mairie »
ou « hôtel de ville », puis le plus grand bâtiment. À défaut de mairie : point intérieur de la commune,
signalé « à vérifier ».

Usage (depuis france/) : .venv/Scripts/python.exe -I -X utf8 chefs_lieux.py
"""
import csv
import json
import sys
from pathlib import Path

import shapely
import shapely.geometry

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI)]
from lieux import choisir_mairie  # noqa: E402  (commun avec le départ par défaut de tuiler.py)
from tuiler import connecter  # noqa: E402  (extension spatial sur D:)

COG = ICI / "cache" / "v_departement.csv"
PARQUETS = str(ICI / "cache" / "regions" / "*.parquet")
SORTIE = ICI / "chefs_lieux.json"
CONTOURS = ICI / "tuiles" / "departements.json"


def main():
    con = connecter()
    deps = [l for l in csv.DictReader(open(COG, encoding="utf-8")) if len(l["DEP"]) == 2]   # métropole : 01..95, 2A, 2B
    codes = [d["CHEFLIEU"] for d in deps]
    # Deux lectures seulement des 2,2 Go : toutes les communes chefs-lieux, puis toutes les mairies de France
    communes = {r[0]: (r[1], shapely.from_wkb(bytes(r[2]))) for r in con.execute(f"""
        SELECT tags['ref:INSEE'], tags['name'], ST_AsWKB(geometry) FROM read_parquet('{PARQUETS}')
        WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '8'
          AND list_contains(?, tags['ref:INSEE']) AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')""",
        [codes]).fetchall()}
    mairies = [(t, shapely.from_wkb(bytes(b))) for t, b in con.execute(f"""
        SELECT tags, ST_AsWKB(geometry) FROM read_parquet('{PARQUETS}') WHERE tags['amenity'] = 'townhall'""").fetchall()]
    arbre = shapely.STRtree([g.centroid for _, g in mairies])
    resultat = []
    for d in deps:
        code = d["CHEFLIEU"]
        if code not in communes:
            print(f"✖ {d['DEP']} : commune {code} introuvable dans OSM")
            continue
        nom, poly = communes[code]
        candidats = [mairies[i] for i in arbre.query(poly, predicate="contains")]
        if candidats:
            g, raison = choisir_mairie(candidats)
            pt = g.centroid
        else:
            pt, raison = poly.representative_point(), "à vérifier : aucune mairie OSM, point intérieur de la commune"
        resultat.append({"dep": d["DEP"], "departement": d["LIBELLE"], "code": code, "commune": nom,
                         "lon": round(pt.x, 6), "lat": round(pt.y, 6), "choix": raison, "candidats": len(candidats)})
        print(f"{d['DEP']:>3} {nom:<26} {len(candidats):>2} → {raison}")
    SORTIE.write_text(json.dumps(resultat, ensure_ascii=False, indent=1), "utf-8")

    # Contours des départements pour la carte de France (choroplèthe) : simplifiés à ~500 m, 4 décimales
    entites = []
    for code, nom, wkb in con.execute(f"""
        SELECT DISTINCT ON (tags['ref']) tags['ref'], tags['name'], ST_AsWKB(geometry) FROM read_parquet('{PARQUETS}')
        WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '6'
          AND list_contains(?, tags['ref']) AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')""",
            [[d["DEP"] for d in deps]]).fetchall():
        g = shapely.set_precision(shapely.simplify(shapely.from_wkb(bytes(wkb)), 0.005), 0.0001)
        entites.append({"type": "Feature", "properties": {"dep": code, "nom": nom}, "geometry": shapely.geometry.mapping(g)})
    # Départements sans polygone OSM exploitable (Rhône au niveau 5 depuis la Métropole de Lyon ;
    # Loire-Atlantique, Marne : relation non reconstruite par QuackOSM) : union de leurs communes
    presents = {e["properties"]["dep"] for e in entites}
    for d in deps:
        if d["DEP"] in presents:
            continue
        communes_dep = [shapely.from_wkb(bytes(r[0])) for r in con.execute(f"""
            SELECT ST_AsWKB(geometry) FROM read_parquet('{PARQUETS}')
            WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '8' AND starts_with(tags['ref:INSEE'], ?)
              AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')""", [d["DEP"]]).fetchall()]
        if not communes_dep:
            print(f"✖ contour du {d['DEP']} introuvable")
            continue
        g = shapely.set_precision(shapely.simplify(shapely.union_all(shapely.make_valid(communes_dep)), 0.005), 0.0001)
        entites.append({"type": "Feature", "properties": {"dep": d["DEP"], "nom": d["LIBELLE"]}, "geometry": shapely.geometry.mapping(g)})
        print(f"  contour du {d['DEP']} reconstitué à partir de {len(communes_dep)} communes")
    CONTOURS.write_text(json.dumps({"type": "FeatureCollection", "features": entites}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"Écrit : {CONTOURS.name}, {len(entites)} contours, {CONTOURS.stat().st_size / 1024:.0f} Ko")
    print(f"Écrit : {SORTIE.name}, {len(resultat)} chefs-lieux sur {len(deps)} départements")


if __name__ == "__main__":
    main()

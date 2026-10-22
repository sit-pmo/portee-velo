"""Ce que la page dit d'un territoire tuilé : nom et ses formes (« en … », « de … »), contour nettoyé, départ
par défaut. Écrit dans contour.json par tuiler.py (--departement), lu par index.html : la page n'a plus de
texte propre au 17.

Départ par défaut = mairie de la commune la plus peuplée du département (tag population d'OSM) : c'est le
chef-lieu partout où on en a besoin (La Rochelle, Saint-Denis, Cayenne, Fort-de-France, Mamoudzou), sauf
en Guadeloupe (Les Abymes, plus peuplée que Basse-Terre), ce qui convient aussi pour un départ.
"""
import re

import numpy as np
import shapely

# Formes du nom pour les phrases de la page : où (« en Charente-Maritime »), de quoi (« de la Charente-Maritime »),
# sur quoi (« sur la Charente-Maritime »). Les départements métropolitains viendront avec la phase 4 (article
# et genre d'après le COG de l'INSEE) ; à défaut, formes neutres construites sur le nom OSM.
FORMES = {
    "fr":  ("en France métropolitaine", "de la France métropolitaine", "sur la France métropolitaine"),
    "17":  ("en Charente-Maritime", "de la Charente-Maritime", "sur la Charente-Maritime"),
    "971": ("en Guadeloupe", "de la Guadeloupe", "sur la Guadeloupe"),
    "972": ("en Martinique", "de la Martinique", "sur la Martinique"),
    "973": ("en Guyane", "de la Guyane", "sur la Guyane"),
    "974": ("à La Réunion", "de La Réunion", "sur La Réunion"),
    "976": ("à Mayotte", "de Mayotte", "sur Mayotte"),
}
SURFACE_MIN_KM2 = 0.05   # parties du contour plus petites (îlots, rochers, carrés de 4 points) : retirées


def formes(code, nom):
    """{"en", "de", "sur"} pour le département code ; repli : « dans le département de Nom », etc."""
    en, de, sur = FORMES.get(code, (f"dans le département {nom}", f"du département {nom}", f"sur le département {nom}"))
    return {"en": en, "de": de, "sur": sur}


def nettoyer_contour(geom, vers_metres, surface_min_km2=SURFACE_MIN_KM2):
    """Retire d'un (Multi)Polygon les parties de moins de surface_min_km2 (calculées dans la projection
    métrique du territoire). La relation OSM de La Réunion compte 5 rochers qui, simplifiés à ~50 m,
    deviennent des carrés de 4 points. Garde au moins la plus grande partie."""
    parties = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    surf = [shapely.transform(p, lambda xy: np.column_stack(vers_metres.transform(xy[:, 0], xy[:, 1]))).area / 1e6
            for p in parties]
    garder = [p for p, s in zip(parties, surf) if s >= surface_min_km2] or [parties[int(np.argmax(surf))]]
    return garder[0] if len(garder) == 1 else shapely.MultiPolygon(garder)


def choisir_mairie(candidats):
    """candidats : liste de (tags, géométrie). Renvoie (géométrie, raison).

    Dans les grandes villes il y a plusieurs mairies (arrondissements, annexes, quartiers) : on veut la
    mairie centrale. Ordre : nom « hôtel de ville », townhall:type=city, nom « mairie » sans mention
    d'annexe / arrondissement / quartier / secteur, puis le plus grand bâtiment.
    """
    def rang(c):
        tags, g = c
        nom = (tags.get("name") or "").lower()
        secondaire = bool(re.search(r"annexe|antenne|direction|arrondissement|quartier|secteur|\d+(e|er|ème)|délégu|ancien", nom))   # « Ancienne mairie de Fort-de-France »
        # Le nom passe avant townhall:type=city, parfois posé sur des mairies de quartier (Nantes Chantenay)
        return (bool(re.search(r"h[ôo]tel[ -]de[ -]ville", nom)) and not secondaire,
                tags.get("townhall:type") == "city" and not secondaire,
                bool(re.search(r"mairie", nom)) and not secondaire,
                not secondaire,
                g.area)
    meilleur = max(candidats, key=rang)
    r = rang(meilleur)
    raison = ("hôtel de ville" if r[0] else "townhall:type=city" if r[1] else "mairie" if r[2]
              else "plus grand (aucun nom clair)")
    return meilleur[1], f"{raison} : {meilleur[0].get('name', 'sans nom')}"


def population(tags):
    """Tag population d'OSM en entier (« 77 205 », « 77205 ») ; 0 s'il manque ou est illisible."""
    try:
        return int(re.sub(r"[\s  .]", "", tags.get("population") or "0"))
    except ValueError:
        return 0


def depart_par_defaut(con, parquets, code, dep):
    """Mairie de la commune la plus peuplée du département (géométrie dep, WGS84) :
    {"commune", "lon", "lat", "choix"}, ou None si aucune commune n'est trouvée."""
    src = repr([str(p) for p in parquets])
    communes = [(t, shapely.from_wkb(bytes(b))) for t, b in con.execute(f"""
        SELECT tags, ST_AsWKB(geometry) FROM read_parquet({src})
        WHERE tags['boundary'] = 'administrative' AND tags['admin_level'] = '8'
          AND starts_with(tags['ref:INSEE'], ?) AND ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')""",
        [code]).fetchall()]
    communes = [(dict(t), g) for t, g in communes if dep.intersects(g.representative_point())]
    if not communes:
        return None
    tags, poly = max(communes, key=lambda c: (population(c[0]), c[1].area))
    w, s, e, n = poly.bounds
    mairies = [(dict(t), shapely.from_wkb(bytes(b))) for t, b in con.execute(f"""
        SELECT tags, ST_AsWKB(geometry) FROM read_parquet({src})
        WHERE tags['amenity'] = 'townhall' AND ST_Intersects(geometry, ST_MakeEnvelope(?, ?, ?, ?))""",
        [w, s, e, n]).fetchall()]
    candidats = [m for m in mairies if poly.contains(m[1].centroid)]
    if candidats:
        g, raison = choisir_mairie(candidats)
        pt = g.centroid
    else:
        pt, raison = poly.representative_point(), "aucune mairie OSM : point intérieur de la commune"
    return {"commune": tags.get("name"), "lon": round(pt.x, 6), "lat": round(pt.y, 6), "choix": raison}

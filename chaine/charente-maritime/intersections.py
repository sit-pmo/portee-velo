"""Croisements du réseau cyclable avec les routes, et limitation de vitesse de la route croisée.

Sortie : tuiles/intersections.json, lu par index.html (onglet Confort, boutons « Croisements avec les RD » et
« Croisements avec les autres routes »). À relancer après chaque tuilage, comme maillons.js.

Méthode :
  1. graphe lu dans les tuiles du site (réseau des collectivités déjà intégré sur Marennes-Oléron) ;
  2. croisement = nœud où se touchent une piste ou voie verte (classe 0) ou une bande (classe 1) et une route
     (classe ≥ 2). Pour une bande, la route qui la prolonge (arête à plus de 150° d'une arête de bande : la même
     chaussée, avant ou après la bande) n'est pas un croisement ; seules les voies qui la coupent comptent ;
  3. chaque route croisée est rapprochée de sa voie OpenStreetMap (GeoParquet de extraire.py, voie de même
     classe la plus proche du milieu de l'arête, à 5 m au plus) pour lire ref, maxspeed, zone:maxspeed ;
  4. vitesse : maxspeed (ou zone:maxspeed, source:maxspeed, FR:urban…) = « OSM » ; à défaut, présumée :
     zone de rencontre 20, chemins sans objet, sinon 50 en agglomération et 80 hors agglomération. Agglomération
     présumée = au moins AGGLO_RUES_M de rues (classe 2) à moins de AGGLO_RAYON_M du croisement ;
  5. RD = arête marquée par marquer_rd.py (couche des RD du Département dans le 17, à lancer avant) ; numéro
     lu dans la même couche, à défaut ref OSM ;
  6. croisements d'une même catégorie à moins de FUSION_M fusionnés (route à chaussées séparées, raccords des
     collectivités) : on garde le plus rapide.

Usage (depuis chaine/charente-maritime/) :
  .venv/Scripts/python.exe -I -X utf8 intersections.py --parquet cache/tmp/zones.parquet
  (sans --parquet : vitesses toutes présumées et RD devinées par la classe, pour essai)
"""
import argparse
import gzip
import json
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import shapely
from scipy.spatial import cKDTree

ICI = Path(__file__).resolve().parent
import sys                                    # noqa: E402
sys.path.insert(0, str(ICI))                  # -I retire le dossier du script du chemin
from marquer_rd import apparier, charger_rd   # noqa: E402
M_PAR_DEG = 111_320
FUSION_M = 15
AGGLO_RAYON_M = 200
AGGLO_RUES_M = 800
APPARIEMENT_M = 5
OPPOSE_COS = np.cos(np.radians(150))          # arête « dans le prolongement » : angle de plus de 150°
PARALLELE_DEG = 40                            # route qui part à moins de 40° d'une arête cyclable : elle la longe
PARALLELE_COS = np.cos(np.radians(PARALLELE_DEG))
LONGE_M = 12                                  # route qui reste à moins de 12 m d'une voie cyclable : elle la longe
LONGE_PORTEE_M = 80                           # longueur de voie cyclable examinée de part et d'autre du nœud
COTE_M = 8                                    # voie cyclable à plus de 8 m de part et d'autre de la route : elle la traverse
PORTEE_DIR_M = 40                             # direction d'une voie lue à 40 m du nœud (tracés en dents de scie)
CHEMINS = {"track", "path", "footway", "pedestrian", "bridleway", "steps", "cycleway"}
# highway OSM → classe des tuiles (classer_fin de tuiler.py) : sert à n'apparier qu'une voie de même classe
CLASSE_HIGHWAY = {"residential": 2, "living_street": 2, "service": 2, "road": 2, "secondary": 3, "secondary_link": 3,
                  "primary": 4, "primary_link": 4, "trunk": 4, "trunk_link": 4, "unclassified": 5,
                  "track": 6, "path": 6, "footway": 6, "pedestrian": 6, "bridleway": 6, "tertiary": 7, "tertiary_link": 7}
ZONES = {"FR:URBAN": 50, "FR:RURAL": 80, "FR:ZONE30": 30, "FR:30": 30, "FR:20": 20, "FR:LIVING_STREET": 20,
         "FR:MOTORWAY": 130, "FR:WALK": 6}


# ---------------------------------------------------------------- tuiles
def varints(o):
    """Toutes les valeurs LEB128 d'un tableau d'octets, en une passe numpy (float64 : exact jusqu'à 2^53)."""
    fin = o < 128
    groupe = np.concatenate([[0], np.cumsum(fin)[:-1]])
    debut = np.concatenate([[0], np.flatnonzero(fin)[:-1] + 1])
    rang = np.arange(len(o)) - debut[groupe]
    return np.bincount(groupe, weights=(o & 127).astype(np.float64) * 128.0 ** rang)


def dezz(u):
    return np.where(u % 2 == 1, -(u + 1) / 2, u / 2)


def lire_tuile(chemin):
    """Nœuds et arêtes d'une tuile (format_tuile.py, décodeur decoderTuile de coeur.js)."""
    o = np.frombuffer(gzip.decompress(chemin.read_bytes()), dtype=np.uint8)
    if bytes(o[:4]) != b"PVT1":
        raise ValueError(f"{chemin} : en-tête PVT1 absent")
    v = varints(o[4:])
    if v[0] != 1:
        raise ValueError(f"{chemin} : version {v[0]} inconnue")
    base, n, m = int(v[3]), int(v[4]), int(v[5])
    p = 8
    lon = np.cumsum(dezz(v[p:p + n])) / 1e5; p += n
    lat = np.cumsum(dezz(v[p:p + n])) / 1e5; p += n
    e = v[p:p + 4 * m].reshape(m, 4)
    a = base + np.cumsum(e[:, 0])
    b = a + dezz(e[:, 1])
    at = e[:, 3].astype(np.int64)
    return base, lon, lat, a.astype(np.int64), b.astype(np.int64), e[:, 2] / 10, at & 7, at >> 15   # bit 15 : RD


def charger_graphe(dossier):
    index = json.loads((dossier / "tuiles.json").read_text("utf-8"))
    n_total = max(t[2] + t[3] for t in index["tuiles"])
    lon, lat = np.full(n_total, np.nan), np.full(n_total, np.nan)
    A, B, L, C, R = [], [], [], [], []
    for tx, ty, base, n in index["tuiles"]:
        if n == 0:
            continue
        b0, lo, la, a, b, l, c, r = lire_tuile(dossier / f"{tx}_{ty}.bin")
        lon[b0:b0 + len(lo)], lat[b0:b0 + len(la)] = lo, la
        A.append(a); B.append(b); L.append(l); C.append(c); R.append(r)
    a, b, l, c, r = map(np.concatenate, (A, B, L, C, R))
    garde = (b >= 0) & (b < n_total)
    return index, lon, lat, a[garde], b[garde], l[garde], c[garde], r[garde]


# ---------------------------------------------------------------- croisements
def distance_polyligne(p, t):
    """Distance du point p (x, y) à la polyligne t (k × 2), en mètres."""
    a, b = t[:-1], t[1:]
    ab = b - a
    n2 = (ab ** 2).sum(1)
    u = np.clip(((p - a) * ab).sum(1) / np.where(n2 > 0, n2, 1), 0, 1)
    return float(np.min(np.hypot(*(a + u[:, None] * ab - p).T)))


def croisements(lon, lat, a, b, cls, lm):
    """Liste de (nœud, type 0 piste / 1 bande, arêtes de route croisées).

    Deux faux croisements écartés :
      - bande : la route dans son prolongement (à plus de 150°) est sa propre chaussée ;
      - piste ou bande qui longe une route sans en avoir exactement le tracé (piste des collectivités dessinée à
        côté d'un chemin OSM, par exemple) : les deux traits se recoupent en zigzag et chaque recoupement fait un
        nœud. Une route qui part dans la même direction qu'une arête cyclable (moins de PARALLELE_DEG, directions
        prises à PORTEE_DIR_M le long des voies) la longe : ce n'est pas un croisement."""
    n = len(lon)
    cyc = cls <= 1
    a_cyc = np.zeros(n, bool); a_cyc[a[cyc]] = True; a_cyc[b[cyc]] = True
    a_rte = np.zeros(n, bool); a_rte[a[~cyc]] = True; a_rte[b[~cyc]] = True
    est_cand = a_cyc & a_rte
    # Adjacence complète (CSR) : il faut suivre les voies au-delà du nœud pour en lire la direction
    m = len(a)
    ext = np.concatenate([a, b]); vers = np.concatenate([b, a]); ar = np.concatenate([np.arange(m), np.arange(m)])
    ordre = np.argsort(ext, kind="stable")
    ext, vers, ar = ext[ordre], vers[ordre], ar[ordre]
    off = np.searchsorted(ext, np.arange(n + 1))
    coslat = np.cos(np.radians(lat))

    def bout(nd, e, autre, portee=None):
        """Point atteint en suivant la voie depuis nd par l'arête e, sur PORTEE_DIR_M au plus. Les simples points de
        tracé sont suivis quel que soit l'angle (dents de scie de quelques mètres) ; à un carrefour, on
        continue tout droit (arête de même famille cyclable / route la plus alignée, à moins de 45°) : une arête
        de raccord de quelques décimètres ne donne pas à elle seule la direction de la route."""
        famille, dist, prec, cur, ecur = cyc[e], lm[e], nd, autre, e
        portee = portee or PORTEE_DIR_M
        chemin = [nd, autre]
        for _ in range(80):
            if dist >= portee:
                break
            hx, hy = (lon[cur] - lon[prec]) * coslat[nd], lat[cur] - lat[prec]
            hn = np.hypot(hx, hy)
            meilleur, cmax = None, np.cos(np.radians(45))
            suite = [k for k in range(off[cur], off[cur + 1]) if ar[k] != ecur and cyc[ar[k]] == famille]
            if len(suite) == 1 and off[cur + 1] - off[cur] == 2:
                meilleur = suite[0]                  # simple point de tracé : on suit, quel que soit l'angle
            for k in suite if meilleur is None else []:
                vx, vy = (lon[vers[k]] - lon[cur]) * coslat[nd], lat[vers[k]] - lat[cur]
                vn = np.hypot(vx, vy)
                cs = (hx * vx + hy * vy) / (hn * vn) if hn > 0 and vn > 0 else 1.0
                if cs > cmax:
                    meilleur, cmax = k, cs
            if meilleur is None:
                break
            ecur = ar[meilleur]
            prec, cur = cur, vers[meilleur]
            dist += lm[ecur]
            chemin.append(cur)
        return chemin

    def direction(nd, e, autre):
        o = bout(nd, e, autre)[-1]
        v = np.array([(lon[o] - lon[nd]) * coslat[nd], lat[o] - lat[nd]])
        d = np.hypot(*v)
        return v / d if d > 0 else v

    out, longent = [], 0
    for nd in np.flatnonzero(est_cand):
        aretes = [(int(ar[k]), int(vers[k])) for k in range(off[nd], off[nd + 1])]
        pistes = [x for x in aretes if cls[x[0]] == 0]
        bandes = [x for x in aretes if cls[x[0]] == 1]
        routes = [x for x in aretes if cls[x[0]] >= 2]
        dc = [direction(nd, e, o) for e, o in pistes + bandes]
        # tracés cyclables autour du nœud (LONGE_PORTEE_M de chaque côté), en mètres autour du nœud
        def plan(ids):
            ids = np.asarray(ids)
            return np.column_stack([(lon[ids] - lon[nd]) * coslat[nd] * M_PAR_DEG, (lat[ids] - lat[nd]) * M_PAR_DEG])
        traces = [plan(bout(nd, e, o, LONGE_PORTEE_M)) for e, o in pistes + bandes]

        def longe(e, o):
            """La route reste-t-elle collée à une voie cyclable ? angle de départ, ou point à PORTEE_DIR_M sur la
            route à moins de LONGE_M d'un tracé cyclable."""
            if any(direction(nd, e, o) @ d >= PARALLELE_COS for d in dc):
                return True
            p = plan([bout(nd, e, o)[-1]])[0]
            if np.hypot(*p) < PORTEE_DIR_M / 2:          # route trop courte pour juger (impasse)
                return False
            return any(distance_polyligne(p, t) < LONGE_M for t in traces if len(t) >= 2)
        def change_de_cote():
            """La voie cyclable passe-t-elle d'un côté à l'autre de la route ? Points à LONGE_PORTEE_M sur deux
            branches cyclables, de part et d'autre du tracé de la route (deux branches les plus opposées, sur
            LONGE_PORTEE_M), chacun à COTE_M au moins : piste qui longe la route puis la traverse en biais (D 273 à
            l'Îleau, 14/10/2026). Un zigzag sur le même tracé reste à moins de COTE_M de la route."""
            if len(pistes) + len(bandes) < 2 or len(routes) < 2:
                return False
            dr = [direction(nd, e, o) for e, o in routes]
            i, j = min(((i, j) for i in range(len(routes)) for j in range(i + 1, len(routes))), key=lambda ij: dr[ij[0]] @ dr[ij[1]])
            r = np.vstack([plan(bout(nd, *routes[i], LONGE_PORTEE_M))[::-1], plan(bout(nd, *routes[j], LONGE_PORTEE_M))[1:]])
            cotes = set()
            for t in traces:
                q = t[-1]
                s0, s1 = r[:-1], r[1:]
                ab = s1 - s0
                n2 = (ab ** 2).sum(1)
                u = np.clip(((q - s0) * ab).sum(1) / np.where(n2 > 0, n2, 1), 0, 1)
                d = np.hypot(*(s0 + u[:, None] * ab - q).T)
                k = int(np.argmin(d))
                if d[k] >= COTE_M:
                    cotes.add(np.sign(ab[k, 0] * (q[1] - s0[k, 1]) - ab[k, 1] * (q[0] - s0[k, 0])))
            return len(cotes) == 2
        avant = len(routes)
        if not change_de_cote():
            routes = [(e, o) for e, o in routes if not longe(e, o)]
        if not pistes:
            # bande seule : la route dans le prolongement d'une bande est sa propre chaussée
            db = [direction(nd, e, o) for e, o in bandes]
            routes = [(e, o) for e, o in routes if all(direction(nd, e, o) @ d > OPPOSE_COS for d in db)]
        if avant and not routes:
            longent += 1
        if routes:
            out.append((nd, 0 if pistes else 1, routes))
    print(f"  {longent} nœuds écartés : la voie cyclable y longe la route ou en est la chaussée")
    return out


# ---------------------------------------------------------------- tags OSM
def lire_osm(parquets, emprise):
    """Voies routières OSM de l'emprise : (géométries Lambert 93, classes, tags utiles)."""
    import duckdb
    from pyproj import Transformer
    # Sans l'extension spatial (rien à télécharger) : géométries lues en WKB brut, emprise filtrée par shapely
    con = duckdb.connect()
    con.execute("SET enable_geoparquet_conversion = false")
    res = con.execute(f"""
        SELECT tags['highway'], tags['ref'], tags['maxspeed'], tags['zone:maxspeed'], tags['source:maxspeed'],
               tags['maxspeed:type'], tags['name'], geometry
        FROM read_parquet({[str(p) for p in parquets]!r})
        WHERE tags['highway'] IN ({", ".join(repr(h) for h in CLASSE_HIGHWAY)})""").fetchall()
    g_tous = shapely.from_wkb([bytes(r[7]) for r in res])
    bx = shapely.bounds(g_tous)
    w, s, e, n = emprise
    dans = (bx[:, 2] >= w) & (bx[:, 0] <= e) & (bx[:, 3] >= s) & (bx[:, 1] <= n)
    res = [r[:7] + (g,) for r, g, d in zip(res, g_tous, dans) if d]
    vers_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
    geoms, classes, tags = [], [], []
    for hw, ref, ms, zms, sms, mst, nom, g in res:
        hw0 = hw[0] if isinstance(hw, list) else hw
        if g is None or g.geom_type not in ("LineString", "Polygon"):
            continue
        if g.geom_type == "Polygon":
            g = g.exterior
        xy = np.asarray(g.coords)[:, :2]
        x, y = vers_l93.transform(xy[:, 0], xy[:, 1])
        geoms.append(shapely.linestrings(np.column_stack([x, y])))
        classes.append(CLASSE_HIGHWAY[hw0])
        un = lambda t: t[0] if isinstance(t, list) and t else (t or None)   # QuackOSM : map → liste de valeurs
        tags.append({"highway": hw0, "ref": un(ref), "maxspeed": un(ms), "zone": un(zms) or un(sms) or un(mst), "name": un(nom)})
    print(f"  {len(geoms)} voies OSM lues")
    return np.array(geoms), np.array(classes), tags, vers_l93


def vitesse_osm(t):
    """km/h lus dans les tags (maxspeed, sinon zone:maxspeed / source:maxspeed / maxspeed:type), None sinon."""
    for cle in ("maxspeed", "zone"):
        v = (t.get(cle) or "").strip().upper().replace(" ", "")
        if not v:
            continue
        m = re.match(r"^(\d+)", v)
        if m:
            return int(m.group(1))
        if v in ZONES:
            return ZONES[v]
    return None


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tuiles", default=str(ICI.parents[1] / "marennes-oleron" / "tuiles"), help="dossier des tuiles du site (défaut ../../marennes-oleron/tuiles)")
    ap.add_argument("--parquet", action="append", default=[], help="GeoParquet OSM (extraire.py, ou cache/tmp/zones.parquet)")
    ap.add_argument("--sortie", help="fichier de sortie (défaut <tuiles>/intersections.json)")
    ap.add_argument("--rd", default=str(ICI / "cache" / "geosir-routes.geojson"), help="RD du Département (numéros)")
    args = ap.parse_args()
    t0 = time.time()
    dossier = Path(args.tuiles)
    index, lon, lat, a, b, l, cls, est_rd = charger_graphe(dossier)
    if not est_rd.any():
        raise SystemExit("aucune arête marquée RD : lancer d'abord marquer_rd.py sur ces tuiles")
    print(f"graphe : {np.isfinite(lon).sum()} nœuds, {len(a)} arêtes ({time.time() - t0:.0f} s)")
    liste = croisements(lon, lat, a, b, cls, l)
    print(f"{len(liste)} nœuds de croisement")

    # Agglomération présumée : longueur de rues (classe 2) autour du croisement (milieux d'arêtes)
    coslat0 = np.cos(np.radians(46))
    def plan(lo, la):
        return np.column_stack([lo * coslat0 * M_PAR_DEG, la * M_PAR_DEG])
    rues = np.flatnonzero(cls == 2)
    arbre_rues = cKDTree(plan((lon[a[rues]] + lon[b[rues]]) / 2, (lat[a[rues]] + lat[b[rues]]) / 2))
    l_rues = l[rues]

    # Appariement des routes croisées aux voies OSM
    toutes = sorted({e for _n, _t, rs in liste for e, _o in rs})
    tags_arete = {}
    if args.parquet:
        emprise = (np.nanmin(lon), np.nanmin(lat), np.nanmax(lon), np.nanmax(lat))
        geoms, classes, tags, vers_l93 = lire_osm(args.parquet, emprise)
        arbre = shapely.STRtree(geoms)
        e = np.array(toutes)
        mx, my = vers_l93.transform((lon[a[e]] + lon[b[e]]) / 2, (lat[a[e]] + lat[b[e]]) / 2)
        pts = shapely.points(mx, my)
        # plusieurs candidats à 5 m : on prend le plus proche de même classe
        idx_pt, idx_g = arbre.query(pts, predicate="dwithin", distance=APPARIEMENT_M)
        dist = shapely.distance(pts[idx_pt], geoms[idx_g])
        meilleur = {}
        for ip, ig, d in zip(idx_pt, idx_g, dist):
            bonne = classes[ig] == cls[e[ip]]
            cle = (not bonne, d)
            if ip not in meilleur or cle < meilleur[ip][0]:
                meilleur[ip] = (cle, ig)
        for ip, (_c, ig) in meilleur.items():
            tags_arete[int(e[ip])] = tags[ig]
        print(f"  {len(tags_arete)} / {len(e)} routes croisées appariées à OSM")

    # Numéro des RD croisées : couche du Département (même appariement que marquer_rd.py) ; à défaut (hors du 17
    # ou écart de tracé), ref OSM
    num_rd = {}
    if Path(args.rd).exists():
        from pyproj import Transformer
        rd = charger_rd(args.rd)
        e = np.array([x for x in toutes if est_rd[x]])
        if len(e):
            t = Transformer.from_crs(4326, 2154, always_xy=True)
            x1, y1 = t.transform(lon[a[e]], lat[a[e]])
            x2, y2 = t.transform(lon[b[e]], lat[b[e]])
            seg = apparier(rd, x1, y1, x2, y2)
            num_rd = {int(x): rd[2][s] for x, s in zip(e, seg) if s >= 0}
        print(f"  {len(num_rd)} / {len(e)} RD croisées numérotées par la couche du Département")

    # Un enregistrement par (nœud, catégorie RD / autres) : la route la plus rapide de la catégorie
    enreg = []
    for nd, typ, routes in liste:
        p = plan(np.array([lon[nd]]), np.array([lat[nd]]))[0]
        voisins = arbre_rues.query_ball_point(p, AGGLO_RAYON_M)
        agglo = l_rues[voisins].sum() >= AGGLO_RUES_M
        par_cat = {}
        for e, _o in routes:
            t = tags_arete.get(e, {})
            c = int(cls[e])
            hw = t.get("highway")
            rd = bool(est_rd[e])
            v = vitesse_osm(t)
            if v is not None:
                src = 0                                            # OSM
            elif hw == "living_street":
                v, src = 20, 1
            elif (hw in CHEMINS) if hw else c == 6:
                v, src = 0, 2                                      # chemin : sans objet
            else:
                v, src = (50 if agglo else 80), 1                  # présumée
            cat = 0 if rd else 1
            rang = (v if src != 2 else -1, src == 0)
            if cat not in par_cat or rang > par_cat[cat][0]:
                par_cat[cat] = (rang, v, src, c, (num_rd.get(e) or t.get("ref") or "") if rd else (t.get("name") or ""))
        for cat, (_r, v, src, c, nom) in par_cat.items():
            enreg.append((nd, cat, typ, v, src, c, nom))

    # Département seul (contour.json du site) : la marge de 25 km des tuiles ne sert qu'au calcul des trajets
    contour = dossier / "contour.json"
    if contour.exists():
        dep = shapely.geometry.shape(json.loads(contour.read_text("utf-8"))["geometrie"]).buffer(0.002)
        shapely.prepare(dep)
        enreg = [r for r in enreg if shapely.contains_xy(dep, lon[r[0]], lat[r[0]])]
    # Fusion des croisements proches de même catégorie (on garde le plus rapide, une piste avant une bande)
    enreg.sort(key=lambda r: (-(r[3] if r[4] != 2 else -1), r[2]))
    gardes = []
    for cat in (0, 1):
        rs = [r for r in enreg if r[1] == cat]
        if not rs:
            continue
        xy = plan(lon[[r[0] for r in rs]], lat[[r[0] for r in rs]])
        arbre = cKDTree(xy)
        pris = np.zeros(len(rs), bool)
        for i, r in enumerate(rs):
            if pris[i]:
                continue
            pris[arbre.query_ball_point(xy[i], FUSION_M)] = True
            gardes.append(r)
    gardes.sort(key=lambda r: (r[1], lon[r[0]]))
    noms = sorted({r[6] for r in gardes if r[6]})
    inom = {s: i + 1 for i, s in enumerate(noms)}
    sortie = {
        "version": 1,
        "genere": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "osm": index.get("genere"),
        "tags": bool(args.parquet),
        "agglo": {"rayonM": AGGLO_RAYON_M, "ruesM": AGGLO_RUES_M},
        "noms": [""] + noms,
        # colonnes : lon, lat (1e-5 degré), c catégorie (0 RD, 1 autres), t type (0 piste, 1 bande),
        # v km/h (0 : chemin), s source (0 OSM, 1 présumée, 2 chemin), k classe de la route, n nom ou ref
        "lon": [int(round(lon[r[0]] * 1e5)) for r in gardes],
        "lat": [int(round(lat[r[0]] * 1e5)) for r in gardes],
        "c": [r[1] for r in gardes], "t": [r[2] for r in gardes], "v": [r[3] for r in gardes],
        "s": [r[4] for r in gardes], "k": [r[5] for r in gardes], "n": [inom.get(r[6], 0) for r in gardes],
    }
    chemin = Path(args.sortie) if args.sortie else dossier / "intersections.json"
    chemin.write_text(json.dumps(sortie, ensure_ascii=False, separators=(",", ":")), "utf-8")
    nb = lambda f: sum(1 for r in gardes if f(r))
    print(f"{len(gardes)} croisements : {nb(lambda r: r[1] == 0)} avec une RD, {nb(lambda r: r[1] == 1)} avec d'autres voies ; "
          f"vitesse OSM {nb(lambda r: r[4] == 0)}, présumée {nb(lambda r: r[4] == 1)}, chemins {nb(lambda r: r[4] == 2)}")
    print(f"→ {chemin} ({chemin.stat().st_size / 1024:.0f} Ko, {time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()

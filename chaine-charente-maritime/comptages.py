"""Comptages rattachés à la grille d'« À portée de vélo » : équipements (BPE), habitants et résidences principales
(Filosofi carroyé), et toute couche de points pondérés (emplacements de camping, lits touristiques, RS…).

Produit tuiles/accessibilite.json, lu par index.html : pour chaque départ, la page additionne ce qui tombe dans
les cellules atteintes à 15 et 30 min (même grille de 100 m que les temps).

Exemple :
    python comptages.py --tuiles ../marennes-oleron/tuiles \
        --bpe BPE24.zip \
        --filosofi Filosofi2019_carreaux_200m_gpkg.zip \
        --points "Emplacements de camping=campings.csv:emplacements" \
        --sortie ../marennes-oleron/tuiles/accessibilite.json

Entrées acceptées
- BPE : le zip téléchargé sur insee.fr (fichier « ensemble géolocalisé » + son fichier de modalités varmod),
  ou le CSV / parquet seul. Coordonnées LONGITUDE/LATITUDE si présentes, sinon LAMBERT_X/LAMBERT_Y (EPSG 2154,
  ou colonne EPSG). Le fichier de modalités sert à nommer les types dans la page (fonctions : codes dans FONCTIONS).
- --communes : GeoJSON des communes (propriétés code et nom), par ex.
  https://geo.api.gouv.fr/communes?codeDepartement=17&format=geojson&geometry=contour&fields=code,nom
  Chaque cellule habitée reçoit la commune qui contient son centre (diagnostic par commune), écrit dans un fichier
  à part (--sortie-communes, non publié : il ne sert qu'à diagnostic.js).
- Filosofi carroyé (200 m, ou 1 km) : gpkg, csv ou zip ; carreau repéré par idcar_200m (CRS3035RES200mN…E…,
  coin sud-ouest en EPSG 3035) ou, à défaut, par sa géométrie. Colonnes ind (habitants) et men (ménages =
  résidences principales). Chaque carreau est réparti sur les cellules de 100 m qu'il recouvre (100 sous-points).
- --points "Libellé=fichier[:colonne_poids]" (répétable) : csv ou gpkg de points ; lon/lat (ou longitude/latitude,
  x/y en Lambert 93) ; sans colonne de poids, chaque point compte 1.

Ne garde que ce qui tombe dans les tuiles publiées (tuiles.json).
Secret statistique : ne publiez pas ici de données soumises à diffusion restreinte (fichiers fonciers à la cellule).
"""
import argparse
import io
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

DOMAINES = [
    ("A", "Services aux particuliers"),
    ("B", "Commerces"),
    ("C", "Enseignement"),
    ("D", "Santé et action sociale"),
    ("E", "Transports et déplacements"),
    ("F", "Sports, loisirs et culture"),
    ("G", "Tourisme"),
]
# Fonctions du « territoire de la demi-heure » (cadre du 13/10/2026) : quotidien à 15 min, hebdomadaire à 30 min.
# Types BPE par code exact, ou par préfixe terminé par « * » (ex. F1* = tous les équipements sportifs). Un équipement peut
# servir plusieurs fonctions (un supermarché compte pour « alimentation » et « supermarché ») : masque de bits.
# Le script affiche les types retenus pour chaque fonction : à vérifier à chaque nouveau millésime BPE.
FONCTIONS = [
    ("alimentation", "Alimentation", 15, ["B104", "B105", "B201", "B202"]),
    ("boulangerie", "Boulangerie", 15, ["B207"]),
    ("ecole", "École", 15, ["C107", "C108", "C109"]),
    ("medecin", "Médecin généraliste", 30, ["D265"]),
    ("pharmacie", "Pharmacie", 30, ["D307"]),
    ("poste", "Poste", 30, ["A206", "A207", "A208"]),
    ("college", "Collège", 30, ["C201"]),
    ("supermarche", "Supermarché", 30, ["B104", "B105"]),
    # Sport du quotidien : piscine, tennis, terrains et plateaux, salles et gymnases (pas golf, glace, tir, circuits…)
    ("sport", "Équipement sportif", 30, ["F101", "F103", "F111", "F113", "F114", "F116", "F120", "F121", "F129"]),
]

# Corrections de position (--corrections, préparées par corriger_bpe.py) : la BPE n'a pas d'identifiant d'équipement,
# on les repère par type + commune + nom + adresse. Une ligne corrige tous les équipements de même clé (même lieu).
CLE_CORRECTION = ["TYPEQU", "DEPCOM", "NOMRS", "NUMVOIE", "TYPVOIE", "LIBVOIE"]


def cle_correction(ligne):
    """Clé normalisée (textes sans blancs de bord, vides pour les valeurs manquantes)."""
    return tuple("" if pd.isna(ligne[c]) else str(ligne[c]).strip() for c in CLE_CORRECTION)


def appliquer_corrections(df, lon, lat, chemin):
    """Remplace lon/lat des équipements listés dans le CSV (lon/lat vides : position Insee gardée)."""
    cor = pd.read_csv(chemin, dtype=str, keep_default_na=False)
    pos = {}
    for _, r in cor.iterrows():
        if r["lon"].strip() and r["lat"].strip():
            pos[tuple(r[c.lower()].strip() for c in CLE_CORRECTION)] = (float(r["lon"]), float(r["lat"]))
    cles = [cle_correction(r) for r in df[CLE_CORRECTION].to_dict("records")]
    n = 0
    for i, c in enumerate(cles):
        if c in pos:
            lon[i], lat[i] = pos[c]
            n += 1
    print(f"  {n} équipements repositionnés ({len(pos)} corrections dans {Path(chemin).name})")


# ---------------------------------------------------------------- grille
class Grille:
    def __init__(self, dossier_tuiles):
        idx = json.loads((Path(dossier_tuiles) / "tuiles.json").read_text(encoding="utf-8"))
        g = idx["grille"]
        self.lon0, self.lat0, self.dlon, self.dlat, self.C = g["lon0"], g["lat0"], g["dlon"], g["dlat"], g["cellulesTuile"]
        self.tuiles = {(t[0], t[1]) for t in idx["tuiles"]}
        # Largeur de la grille France en cellules : indice linéaire k = gj * W + gi (tient dans un nombre JS)
        self.W = int(round(360 / self.dlon))

    def cellules(self, lon, lat):
        """Indices globaux (gi, gj) et masque « dans une tuile publiée »."""
        gi = np.floor((np.asarray(lon) - self.lon0) / self.dlon).astype(np.int64)
        gj = np.floor((self.lat0 - np.asarray(lat)) / self.dlat).astype(np.int64)
        tx, ty = gi // self.C, gj // self.C
        dedans = np.array([(a, b) in self.tuiles for a, b in zip(tx.tolist(), ty.tolist())], dtype=bool)
        return gi, gj, dedans


def encoder_cellules(grille, gi, gj, valeurs=None):
    """Somme par cellule, triée, indices linéaires en différences (JSON compact)."""
    k = gj * grille.W + gi
    if valeurs is None:
        return k
    df = pd.DataFrame({"k": k, "v": valeurs}).groupby("k", sort=True)["v"].sum()
    df = df[df > 0]
    return df.index.to_numpy(np.int64), df.to_numpy()


def deltas(k):
    k = np.asarray(k, dtype=np.int64)
    return np.diff(k, prepend=0).tolist()


# ---------------------------------------------------------------- lecture
def lire_table(source, nom_interne=None):
    """CSV (séparateur et encodage devinés), parquet ou gpkg ; source = chemin ou (zip, nom)."""
    if isinstance(source, tuple):
        z, nom = source
        donnees = z.read(nom)
        suffixe = Path(nom).suffix.lower()
        if suffixe == ".parquet":
            return pd.read_parquet(io.BytesIO(donnees))
        if suffixe == ".gpkg":
            tmp = Path(".comptages_tmp.gpkg")
            tmp.write_bytes(donnees)
            try:
                return lire_table(tmp)
            finally:
                tmp.unlink(missing_ok=True)
        return lire_csv(donnees)
    p = Path(source)
    suffixe = p.suffix.lower()
    if suffixe == ".parquet":
        return pd.read_parquet(p)
    if suffixe in (".gpkg", ".shp", ".geojson"):
        import pyogrio
        gdf = pyogrio.read_dataframe(p)
        if gdf.crs is not None and gdf.geometry is not None and len(gdf):
            # On garde l'emprise de chaque géométrie en EPSG 3035 / 4326 selon le besoin de l'appelant
            gdf.attrs["crs"] = gdf.crs.to_string()
        return gdf
    return lire_csv(p.read_bytes())


def lire_csv(octets):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            texte = octets.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    premiere = texte.split("\n", 1)[0]
    sep = max([";", ",", "\t", "|"], key=premiere.count)
    return pd.read_csv(io.StringIO(texte), sep=sep, dtype=str, low_memory=False)


def colonne(df, *candidats, obligatoire=True):
    bas = {c.lower(): c for c in df.columns}
    for c in candidats:
        if c.lower() in bas:
            return bas[c.lower()]
    if obligatoire:
        sys.exit(f"Colonne introuvable parmi {candidats} ; colonnes présentes : {list(df.columns)[:30]}")
    return None


def nombre(serie):
    return pd.to_numeric(serie.astype(str).str.replace(",", ".", regex=False).str.strip(), errors="coerce")


def coordonnees(df):
    """lon, lat en WGS84 depuis les colonnes disponibles (géométrie, lon/lat, Lambert 93)."""
    if "geometry" in df.columns and getattr(df, "crs", None) is not None:
        g = df.to_crs(4326).geometry
        c = g.representative_point() if not (g.geom_type == "Point").all() else g
        return c.x.to_numpy(), c.y.to_numpy()
    clon = colonne(df, "longitude", "lon", "lng", obligatoire=False)
    clat = colonne(df, "latitude", "lat", obligatoire=False)
    if clon and clat:
        return nombre(df[clon]).to_numpy(), nombre(df[clat]).to_numpy()
    cx = colonne(df, "lambert_x", "x", "coordx", "x_l93")
    cy = colonne(df, "lambert_y", "y", "coordy", "y_l93")
    x, y = nombre(df[cx]).to_numpy(), nombre(df[cy]).to_numpy()
    lon, lat = np.full(len(df), np.nan), np.full(len(df), np.nan)
    cepsg = colonne(df, "epsg", obligatoire=False)
    epsgs = nombre(df[cepsg]).fillna(2154).astype(int).to_numpy() if cepsg else np.full(len(df), 2154)
    for e in np.unique(epsgs):
        m = (epsgs == e) & np.isfinite(x) & np.isfinite(y)
        if m.any():
            lon[m], lat[m] = Transformer.from_crs(int(e), 4326, always_xy=True).transform(x[m], y[m])
    return lon, lat


# ---------------------------------------------------------------- BPE
def lire_bpe(chemin):
    chemin = Path(chemin)
    varmod = None
    if chemin.suffix.lower() == ".zip":
        z = zipfile.ZipFile(chemin)
        noms = [n for n in z.namelist() if n.lower().endswith((".csv", ".parquet"))]
        vm = [n for n in noms if "varmod" in n.lower() or "modalit" in n.lower()]
        donnees = [n for n in noms if n not in vm]
        if not donnees:
            sys.exit(f"Aucun fichier de données BPE dans {chemin}")
        donnees.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)   # le plus gros = les équipements
        df = lire_table((z, donnees[0]))
        if vm:
            varmod = lire_table((z, vm[0]))
        print(f"BPE : {donnees[0]} ({len(df):,} lignes){' + ' + vm[0] if vm else ''}")
    else:
        df = lire_table(chemin)
        # varmod à côté du fichier ?
        cand = sorted(chemin.parent.glob("*varmod*"))
        if cand:
            varmod = lire_table(cand[0])
        print(f"BPE : {chemin.name} ({len(df):,} lignes){' + ' + cand[0].name if cand else ''}")
    return df, varmod


def libelles_types(varmod):
    if varmod is None:
        return {}
    cvar = colonne(varmod, "cod_var", "variable")
    cmod = colonne(varmod, "cod_mod", "modalite", "code")
    clib = colonne(varmod, "lib_mod", "libelle", "libelle_modalite")
    v = varmod[varmod[cvar].astype(str).str.upper() == "TYPEQU"]
    return dict(zip(v[cmod].astype(str), v[clib].astype(str)))


def traiter_bpe(chemin, grille, corrections=None):
    df, varmod = lire_bpe(chemin)
    ctype = colonne(df, "typequ")
    types = df[ctype].astype(str).str.strip().to_numpy()
    lon, lat = coordonnees(df)
    if corrections:
        lon, lat = np.array(lon, dtype=float), np.array(lat, dtype=float)   # copies modifiables
        appliquer_corrections(df, lon, lat, corrections)
    ok = np.isfinite(lon) & np.isfinite(lat)
    print(f"  {np.count_nonzero(~ok):,} équipements sans coordonnées écartés")
    gi, gj, dedans = grille.cellules(np.where(ok, lon, 0), np.where(ok, lat, 0))
    garde = ok & dedans
    print(f"  {np.count_nonzero(garde):,} équipements dans l'emprise des tuiles")
    libs = libelles_types(varmod)
    if not libs:
        print("  (pas de fichier de modalités : types non nommés)")

    # Fonctions : masque de bits par type
    masque_type = {}
    present = set(types[garde].tolist())
    for b, (fid, flib, seuil, codes) in enumerate(FONCTIONS):
        retenus = sorted(c for c in present if any(c == x or (x.endswith("*") and c.startswith(x[:-1])) for x in codes))
        for c in retenus:
            masque_type[c] = masque_type.get(c, 0) | (1 << b)
        n = int(np.isin(types[garde], retenus).sum())
        noms = ", ".join(f"{c} {libs.get(c, '?')}" for c in retenus)
        print(f"  {flib} ({seuil} min) : {noms or 'AUCUN TYPE TROUVÉ'} → {n} dans l'emprise")

    t = types[garde]
    gi, gj = gi[garde], gj[garde]
    # Position exacte dans la cellule, au centième de cellule (~1 m) : p = 100 × ligne + colonne, depuis le coin nord-ouest
    px = np.clip(np.floor(((lon[garde] - grille.lon0) / grille.dlon - gi) * 100), 0, 99).astype(np.int64)
    py = np.clip(np.floor(((grille.lat0 - lat[garde]) / grille.dlat - gj) * 100), 0, 99).astype(np.int64)
    p = py * 100 + px
    k = gj * grille.W + gi
    ordre = np.argsort(k, kind="stable")
    k, t, p = k[ordre], t[ordre], p[ordre]
    codes_types = sorted(set(t.tolist()))
    itype = {c: i for i, c in enumerate(codes_types)}
    lettres = [d[0] for d in DOMAINES]
    dom = [lettres.index(c[0]) if c[:1] in lettres else -1 for c in t]
    return {
        "k": deltas(k),
        "t": [itype[c] for c in t],
        "d": dom,
        "f": [masque_type.get(c, 0) for c in t],
        "p": p.tolist(),
    },[[c, libs.get(c, c)] for c in codes_types], True


# ---------------------------------------------------------------- Filosofi
def lire_filosofi(chemin):
    chemin = Path(chemin)
    if chemin.suffix.lower() == ".zip":
        z = zipfile.ZipFile(chemin)
        noms = [n for n in z.namelist() if n.lower().endswith((".csv", ".gpkg", ".parquet"))]
        noms = [n for n in noms if "carreau" in n.lower() or "car" in Path(n).name.lower()] or noms
        noms.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
        print(f"Filosofi : {noms[0]}")
        return lire_table((z, noms[0]))
    print(f"Filosofi : {chemin.name}")
    return lire_table(chemin)


def traiter_filosofi(chemin, grille, sous=10):
    df = lire_filosofi(chemin)
    cid = colonne(df, "idcar_200m", "idcar_1km", "idcar", "id", obligatoire=False)
    if cid is not None:
        ids = df[cid].astype(str)
        m = ids.str.extract(r"RES(\d+)m.*?N(\d+)E(\d+)")
        if m.isna().any(axis=None):
            sys.exit(f"Identifiants de carreau illisibles, ex. {ids.iloc[0]!r}")
        res = m[0].astype(float).to_numpy()
        y0, x0 = m[1].astype(float).to_numpy(), m[2].astype(float).to_numpy()
    elif "geometry" in df.columns:
        b = df.to_crs(3035).geometry.bounds
        x0, y0 = b["minx"].to_numpy(), b["miny"].to_numpy()
        res = (b["maxx"] - b["minx"]).round().to_numpy()
    else:
        sys.exit("Filosofi : ni identifiant de carreau ni géométrie")
    couches = {}
    for cle, cands, lib, unite in (("population", ("ind", "ind_c"), "Habitants", "hab."),
                                   ("menages", ("men",), "Résidences principales (ménages)", "")):
        c = colonne(df, *cands, obligatoire=False)
        if c is not None:
            couches[cle] = (nombre(df[c]).fillna(0).to_numpy(), lib, unite)
    if not couches:
        sys.exit("Filosofi : ni colonne ind ni colonne men")

    # Sous-points au centre de sous × sous sous-carreaux, chacun porte 1 / sous² du carreau
    f = (np.arange(sous) + 0.5) / sous
    fx, fy = np.meshgrid(f, f)
    fx, fy = fx.ravel(), fy.ravel()
    t = Transformer.from_crs(3035, 4326, always_xy=True)
    # Préfiltre grossier : centre du carreau dans une tuile publiée (avec marge d'une tuile, les carreaux du bord)
    lonc, latc = t.transform(x0 + res / 2, y0 + res / 2)
    gi, gj, _ = grille.cellules(lonc, latc)
    proches = np.array([any((gi[i] // grille.C + a, gj[i] // grille.C + b) in grille.tuiles
                            for a in (-1, 0, 1) for b in (-1, 0, 1)) for i in range(len(gi))])
    idx = np.nonzero(proches)[0]
    print(f"  {len(idx):,} carreaux dans l'emprise sur {len(df):,}")
    X = (x0[idx, None] + fx[None, :] * res[idx, None]).ravel()
    Y = (y0[idx, None] + fy[None, :] * res[idx, None]).ravel()
    lon, lat = t.transform(X, Y)
    gi, gj, dedans = grille.cellules(lon, lat)
    sorties = {}
    for cle, (v, lib, unite) in couches.items():
        poids = np.repeat(v[idx] / len(fx), len(fx))
        k, s = encoder_cellules(grille, gi[dedans], gj[dedans], poids[dedans])
        total = float(v[idx].sum())
        print(f"  {lib} : {total:,.0f} dans les carreaux retenus, {s.sum():,.0f} sur les cellules des tuiles")
        sorties[cle] = {"id": cle, "libelle": lib, "unite": unite, "source": "Insee, Filosofi carroyé",
                        "echelle": 10, "k": deltas(k), "v": np.round(s * 10).astype(int).tolist()}
    return sorties


# ---------------------------------------------------------------- points pondérés
def traiter_points(spec, grille):
    if "=" not in spec:
        sys.exit(f"--points attend « Libellé=fichier[:colonne] », reçu {spec!r}")
    lib, reste = spec.split("=", 1)
    # Colonne de poids après le dernier « : », sauf s'il s'agit du « C: » d'un chemin Windows
    fichier, cpoids = reste, ""
    if ":" in reste:
        avant, apres = reste.rsplit(":", 1)
        if avant and not re.fullmatch(r"[A-Za-z]", avant) and not re.search(r"[\\/.]", apres):
            fichier, cpoids = avant, apres
    df = lire_table(fichier)
    lon, lat = coordonnees(df)
    if cpoids:
        poids = nombre(df[colonne(df, cpoids)]).fillna(0).to_numpy()
    else:
        poids = np.ones(len(df))
    ok = np.isfinite(lon) & np.isfinite(lat)
    gi, gj, dedans = grille.cellules(np.where(ok, lon, 0), np.where(ok, lat, 0))
    g = ok & dedans
    k, s = encoder_cellules(grille, gi[g], gj[g], poids[g])
    print(f"{lib} : {np.count_nonzero(g):,} points sur {len(df):,} dans l'emprise, total {s.sum():,.0f}")
    ident = re.sub(r"[^a-z0-9]+", "-", lib.lower()).strip("-")
    return {"id": ident, "libelle": lib, "unite": "", "source": Path(fichier).name,
            "echelle": 1, "k": deltas(k), "v": np.round(s).astype(int).tolist()}


# ---------------------------------------------------------------- communes
def assigner_communes(grille, k_deltas, chemin):
    """Commune contenant le centre de chaque cellule habitée (ordre de la couche population) ; -1 si aucune."""
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
    gj = json.loads(Path(chemin).read_text(encoding="utf-8"))
    geoms, liste = [], []
    for f in gj["features"]:
        geoms.append(shape(f["geometry"]))
        liste.append([str(f["properties"]["code"]), f["properties"]["nom"]])
    arbre = STRtree(geoms)
    k = np.cumsum(k_deltas)
    gj_, gi_ = k // grille.W, k % grille.W
    lon = grille.lon0 + (gi_ + .5) * grille.dlon
    lat = grille.lat0 - (gj_ + .5) * grille.dlat
    idx = []
    for x, y in zip(lon, lat):
        p = Point(x, y)
        cand = [i for i in arbre.query(p) if geoms[i].contains(p)]
        idx.append(int(cand[0]) if cand else -1)
    idx = np.array(idx)
    utiles = sorted(set(idx[idx >= 0].tolist()))
    renum = {v: i for i, v in enumerate(utiles)}
    print(f"Communes : {len(utiles)} communes pour {len(idx):,} cellules habitées, {int((idx < 0).sum())} hors commune")
    return {"liste": [liste[i] for i in utiles], "c": [renum.get(int(v), -1) for v in idx]}


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tuiles", required=True, help="dossier des tuiles (tuiles.json)")
    ap.add_argument("--bpe", help="BPE géolocalisée (zip insee.fr, csv ou parquet)")
    ap.add_argument("--corrections", help="CSV de positions corrigées de la BPE (corriger_bpe.py, vérifié à la main)")
    ap.add_argument("--filosofi", help="Filosofi carroyé (zip, gpkg ou csv)")
    ap.add_argument("--communes", help="GeoJSON des communes (code, nom) : commune de chaque cellule habitée")
    ap.add_argument("--sortie-communes", default="communes_cellules.json", help="fichier des communes par cellule habitée")
    ap.add_argument("--points", action="append", default=[], help="« Libellé=fichier[:colonne_poids] »")
    ap.add_argument("--millesime-bpe", default="", help="ex. 2024, affiché dans la page")
    ap.add_argument("--millesime-filosofi", default="", help="ex. 2019")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()

    grille = Grille(a.tuiles)
    out = {"version": 1, "genere": datetime.now().strftime("%Y-%m-%d %H:%M"), "W": grille.W,
           "domaines": [{"code": c, "libelle": l} for c, l in DOMAINES],
           "fonctions": [{"id": f, "libelle": l, "seuil": s} for f, l, s, _ in FONCTIONS],
           "types": [], "equipements": None, "couches": [], "sources": []}
    if a.bpe:
        eq, types, _ = traiter_bpe(a.bpe, grille, a.corrections)
        out["equipements"], out["types"] = eq, types
        out["sources"].append(f"Insee, Base permanente des équipements {a.millesime_bpe}".strip())
    if a.filosofi:
        fil = traiter_filosofi(a.filosofi, grille)
        out["couches"] += list(fil.values())
        if a.communes and "population" in fil:
            com = assigner_communes(grille, fil["population"]["k"], a.communes)
            Path(a.sortie_communes).write_text(json.dumps(com, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            print(f"→ {a.sortie_communes}")
        out["sources"].append(f"Insee, Filosofi {a.millesime_filosofi} carroyé".strip())
    for spec in a.points:
        out["couches"].append(traiter_points(spec, grille))
    if not (a.bpe or a.filosofi or a.points):
        sys.exit("Rien à faire : donnez au moins --bpe, --filosofi ou --points")
    texte = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    Path(a.sortie).write_text(texte, encoding="utf-8")
    print(f"→ {a.sortie} ({len(texte) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()

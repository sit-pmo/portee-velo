"""Format binaire d'une tuile de graphe vélo (version 1) : encodeur Python.

Le décodeur miroir est `decoderTuile()` dans coeur.js ; le test aller-retour est
test/format.test.js (fixture produite par test/fabriquer_fixture.py).

Grille de France (en degrés, comme la grille de coeur.js) :
  - origine nord-ouest (LON0, LAT0), cellule DLON × DLAT ≈ 100 m,
  - tuile = CELLULES_TUILE × CELLULES_TUILE cellules ≈ 10 km ; tuile (tx, ty), ty croît vers le sud.

Contenu (après gzip, que le navigateur défait avec DecompressionStream) :
  "PVT1"                          4 octets magiques
  puis une suite d'entiers varint (LEB128 ; « zz » = zigzag pour les valeurs signées) :
  version, zz(tx), zz(ty), base, n, m, ncx, ncy
  nœuds possédés, identifiants globaux base .. base+n-1 (n valeurs chacun) :
    lon  : zz(delta) en 1e-5 degré, la première valeur relative à 0
    lat  : idem
  arêtes, triées par a (m valeurs chacune) :
    a    : delta avec l'arête précédente (index local du nœud possédé, ≥ 0)
    b    : zz(b_global - (base + a))   — b peut appartenir à une tuile voisine
    l    : longueur en décimètres (< 65 536)
    attr : classe | sens << 3 | vitesse (km/h) << 5, comme aujourd'hui (jusqu'à ~8 000, pas un octet)
  cellules (ncx × ncy, ligne par ligne depuis le nord-ouest) :
    terre     : 0 / 1
    pro_tout  : zz(delta de (id_global + 1)), 0 = aucun nœud
    pro_amen  : idem
    dist_tout : distance au nœud en pas de 10 m, plafonnée à 255
    dist_amen : idem
"""
import gzip

# Une grille par territoire (16/10/2026) : coin nord-ouest (lon0, lat0), DLON ≈ 100 m à la latitude du territoire
# (100 / (111 320 · cos φ)), EPSG métrique pour les aires et le masque terre. DLAT = 0,0009 partout.
# La page lit la grille dans tuiles.json : rien à recopier dans coeur.js.
TERRITOIRES = {
    "fr":  {"nom": "France métropolitaine", "lon0": -5.5,  "lat0": 51.5,  "dlon": 0.00125, "epsg": 2154},
    "971": {"nom": "Guadeloupe",            "lon0": -61.9, "lat0": 16.6,  "dlon": 0.00093, "epsg": 5490},
    "972": {"nom": "Martinique",            "lon0": -61.3, "lat0": 14.9,  "dlon": 0.00093, "epsg": 5490},
    "973": {"nom": "Guyane",                "lon0": -54.7, "lat0": 6.0,   "dlon": 0.00091, "epsg": 2972},
    "974": {"nom": "La Réunion",            "lon0": 55.1,  "lat0": -20.8, "dlon": 0.00096, "epsg": 2975},
    "976": {"nom": "Mayotte",               "lon0": 44.9,  "lat0": -12.5, "dlon": 0.00092, "epsg": 4471},
}
LON0, LAT0 = -5.5, 51.5          # grille courante (choisir() la change) ; par défaut la métropole
DLON, DLAT = 0.00125, 0.0009     # ≈ 97 × 100 m vers 45° N
EPSG = 2154
CELLULES_TUILE = 100             # tuile ≈ 10 km de côté
MAGIQUE = b"PVT1"
VERSION = 1
PAS_DIST_M = 10                  # distance d'accès stockée par pas de 10 m
DIST_MAX = 255                   # 2 550 m ≥ accesMaxM (2 000 m) : au-delà, la cellule est hors d'atteinte


def zigzag(v):
    """Entier signé → non signé (0, -1, 1, -2… → 0, 1, 2, 3…)."""
    return (v << 1) if v >= 0 else ((-v << 1) - 1)


def _varint(v, out):
    """Ajoute l'entier non signé v en LEB128 (7 bits par octet)."""
    if v < 0:
        raise ValueError(f"varint négatif : {v}")
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def _deltas_zz(valeurs, out):
    """Écrit une suite en écarts successifs zigzag (la première relative à 0)."""
    prec = 0
    for v in valeurs:
        _varint(zigzag(v - prec), out)
        prec = v


def encoder_tuile(t):
    """Encode une tuile (dict) en octets gzip.

    Clés attendues : tx, ty, base, lon, lat (listes d'entiers en 1e-5°), a (index locaux),
    b (identifiants globaux), l (dm), attr, ncx, ncy, terre, pro_tout, pro_amen
    (identifiants globaux, -1 = aucun), dist_tout, dist_amen (mètres).
    """
    n, m, nc = len(t["lon"]), len(t["a"]), t["ncx"] * t["ncy"]
    assert len(t["lat"]) == n and len(t["b"]) == m == len(t["l"]) == len(t["attr"])
    for cle in ("terre", "pro_tout", "pro_amen", "dist_tout", "dist_amen"):
        assert len(t[cle]) == nc, f"{cle} : {len(t[cle])} cellules au lieu de {nc}"
    out = bytearray(MAGIQUE)
    for v in (VERSION, zigzag(t["tx"]), zigzag(t["ty"]), t["base"], n, m, t["ncx"], t["ncy"]):
        _varint(v, out)
    _deltas_zz(t["lon"], out)
    _deltas_zz(t["lat"], out)
    # Arêtes triées par nœud de départ : les écarts de a restent petits et positifs
    ordre = sorted(range(m), key=lambda e: t["a"][e])
    prec_a = 0
    for e in ordre:
        a = t["a"][e]
        assert 0 <= a < n, f"arête {e} : a={a} hors des nœuds possédés"
        _varint(a - prec_a, out)
        prec_a = a
        _varint(zigzag(t["b"][e] - (t["base"] + a)), out)
        # Plafond du champ l (Uint16 côté coeur.js) : les arêtes trop longues doivent être coupées avant
        assert 0 <= t["l"][e] < 65536, f"arête {e} : longueur {t['l'][e]} dm hors plafond"
        _varint(t["l"][e], out)
        assert 0 <= t["attr"][e] < 65536, f"arête {e} : attr {t['attr'][e]} hors Uint16 (coeur.js)"
        _varint(t["attr"][e], out)
    for v in t["terre"]:
        _varint(1 if v else 0, out)
    for cle in ("pro_tout", "pro_amen"):
        _deltas_zz([v + 1 for v in t[cle]], out)
    for cle in ("dist_tout", "dist_amen"):
        for d in t[cle]:
            _varint(min(DIST_MAX, round(d / PAS_DIST_M)), out)
    # mtime=0 : même entrée → mêmes octets (dépôt git stable entre deux générations)
    return gzip.compress(bytes(out), compresslevel=9, mtime=0)


def choisir(code):
    """Rend courante la grille du territoire `code` (clé de TERRITOIRES) et renvoie (LON0, LAT0, DLON, DLAT, EPSG)."""
    global LON0, LAT0, DLON, EPSG
    g = TERRITOIRES[code]
    LON0, LAT0, DLON, EPSG = g["lon0"], g["lat0"], g["dlon"], g["epsg"]
    return LON0, LAT0, DLON, DLAT, EPSG


def tuile_de(lon, lat):
    """Tuile (tx, ty) contenant un point en degrés."""
    return (int((lon - LON0) // (DLON * CELLULES_TUILE)), int((LAT0 - lat) // (DLAT * CELLULES_TUILE)))


if __name__ == "__main__":
    # Auto-contrôles minimaux : zigzag, varint, déterminisme
    assert [zigzag(v) for v in (0, -1, 1, -2, 2)] == [0, 1, 2, 3, 4]
    o = bytearray()
    _varint(300, o)
    assert bytes(o) == b"\xac\x02"
    print("format_tuile : auto-contrôles OK, tuile de Saint-Pierre-d'Oléron :", tuile_de(-1.3063, 45.9443))

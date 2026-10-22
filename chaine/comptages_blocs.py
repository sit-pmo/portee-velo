"""Équipements (BPE) et habitants (Filosofi carroyé) par blocs de tuiles (phase 6, 16/10/2026).

comptages.py écrit un seul accessibilite.json pour tout le territoire : bien pour un département, pas pour la
métropole (2,7 M équipements, 2,3 M carreaux). Ici, même calcul (traiter_bpe, traiter_filosofi de comptages.py),
puis découpage par blocs de BLOC × BLOC tuiles : acces/index.json (libellés, fonctions, types, sources, liste des
blocs) et acces/<bx>_<by>.json (entrées du bloc, mêmes colonnes que accessibilite.json). La page ne lit que les
blocs de sa fenêtre et les fusionne (index.html, chargerAccesBlocs).

Filosofi : chaque carreau de 200 m est réparti sur SOUS × SOUS sous-points (4 × 4 au lieu de 10 × 10 : la
métropole en ferait 230 millions) ; une cellule de 100 m reçoit ainsi sa part du carreau à 1/16 près.

Usage (depuis chaine/) :
  python -I -X utf8 comptages_blocs.py --tuiles ../tuiles/fr --bpe BPE24.zip --millesime-bpe 2024 \\
      --filosofi carreaux-200m-met-3035-2021.parquet --millesime-filosofi 2021
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ICI = Path(__file__).resolve().parent
sys.path[:0] = [str(ICI)]
import comptages as K   # noqa: E402

BLOC = 4
SOUS = 4


def blocs_de(grille, k_abs, B):
    """Bloc (bx, by) de chaque entrée, d'après son indice de cellule global k = gj × W + gi."""
    gj, gi = np.divmod(np.asarray(k_abs, np.int64), grille.W)
    return (gi // grille.C) // B, (gj // grille.C) // B


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tuiles", required=True)
    ap.add_argument("--bpe")
    ap.add_argument("--filosofi")
    ap.add_argument("--millesime-bpe", default="")
    ap.add_argument("--millesime-filosofi", default="")
    ap.add_argument("--bloc", type=int, default=BLOC)
    a = ap.parse_args()
    grille = K.Grille(a.tuiles)
    B = a.bloc
    meta = {"version": 1, "genere": datetime.now().strftime("%Y-%m-%d %H:%M"), "W": grille.W, "bloc": B,
            "domaines": [{"code": c, "libelle": l} for c, l in K.DOMAINES],
            "fonctions": [{"id": f, "libelle": l, "seuil": s} for f, l, s, _ in K.FONCTIONS],
            "types": [], "couches": [], "sources": []}
    eq, couches = None, []
    if a.bpe:
        eq, types, _ = K.traiter_bpe(a.bpe, grille)
        meta["types"] = types
        meta["sources"].append(f"Insee, Base permanente des équipements {a.millesime_bpe}".strip())
    if a.filosofi:
        fil = K.traiter_filosofi(a.filosofi, grille, sous=SOUS)
        couches = list(fil.values())
        meta["sources"].append(f"Insee, Filosofi {a.millesime_filosofi} carroyé".strip())
    if eq is None and not couches:
        sys.exit("Rien à faire : donnez --bpe et/ou --filosofi")
    meta["couches"] = [{k: c[k] for k in ("id", "libelle", "unite", "source", "echelle") if k in c} for c in couches]

    # Découpage : pour chaque bloc, les entrées dont la cellule y tombe (k en différences, recalculées par bloc)
    parts = {}
    if eq is not None:
        k = np.cumsum(np.asarray(eq["k"], np.int64))
        bx, by = blocs_de(grille, k, B)
        cols = {c: np.asarray(eq[c]) for c in ("t", "d", "f", "p")}
        for cle in set(zip(bx.tolist(), by.tolist())):
            m = (bx == cle[0]) & (by == cle[1])
            parts.setdefault(cle, {})["equipements"] = {"k": K.deltas(k[m]), **{c: v[m].tolist() for c, v in cols.items()}}
    for c in couches:
        k = np.cumsum(np.asarray(c["k"], np.int64))
        v = np.asarray(c["v"])
        bx, by = blocs_de(grille, k, B)
        for cle in set(zip(bx.tolist(), by.tolist())):
            m = (bx == cle[0]) & (by == cle[1])
            parts.setdefault(cle, {}).setdefault("couches", []).append({"id": c["id"], "k": K.deltas(k[m]), "v": v[m].tolist()})

    sortie = Path(a.tuiles) / "acces"
    shutil.rmtree(sortie, ignore_errors=True)
    sortie.mkdir()
    for (x, y), D in parts.items():
        (sortie / f"{x}_{y}.json").write_text(json.dumps(D, separators=(",", ":")), encoding="utf-8")
    meta["blocs"] = sorted([x, y] for x, y in parts)
    (sortie / "index.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    taille = sum(f.stat().st_size for f in sortie.iterdir()) / 1e6
    print(f"→ {sortie} : {len(parts)} blocs, {taille:.1f} Mo")


if __name__ == "__main__":
    main()

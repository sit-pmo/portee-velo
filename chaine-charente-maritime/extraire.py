"""Étape 1 de la chaîne France : PBF OSM → GeoParquet (QuackOSM / DuckDB).

Garde uniquement ce dont la suite a besoin, avec tous leurs tags (colonne `tags`) :
  - highway=*                      → graphe vélo (règles de build_graph_velo.py)
  - amenity=townhall               → mairies des chefs-lieux (scores)
  - natural=water                  → soustrait du masque terre (estuaires, retenues)
  - boundary=administrative        → limites départementales et communales (admin_level 6 / 8)

Affiche la durée, la RAM maximale et la taille du résultat : c'est la mesure qui dit
si la France entière (~4,5 Go de PBF) passe sur le poste.

Tous les fichiers temporaires (QuackOSM, DuckDB, tempfile) vont dans cache/tmp, à côté du
PBF (disque D:), jamais dans le TEMP de Windows (C:).

Usage : .venv/Scripts/python.exe extraire.py cache/poitou-charentes-261008.osm.pbf [sortie.parquet] [--ram-go 4]
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
import quackosm

# Filtre QuackOSM : True = toutes les valeurs de la clé, sinon valeur(s) exigée(s)
FILTRE_TAGS = {
    "highway": True,
    "amenity": "townhall",
    "natural": "water",
    "boundary": "administrative",
}


class SondeRam:
    """Échantillonne la mémoire résidente du processus (DuckDB tourne dans le même processus)."""

    def __init__(self, periode_s=0.2):
        self.periode_s = periode_s
        self.max_octets = 0
        self._stop = threading.Event()
        self._proc = psutil.Process()
        self._fil = threading.Thread(target=self._boucle, daemon=True)

    def _boucle(self):
        while not self._stop.is_set():
            rss = self._proc.memory_info().rss
            # Les éventuels sous-processus comptent aussi
            for enfant in self._proc.children(recursive=True):
                try:
                    rss += enfant.memory_info().rss
                except psutil.Error:
                    pass
            self.max_octets = max(self.max_octets, rss)
            time.sleep(self.periode_s)

    def __enter__(self):
        self._fil.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._fil.join()


def main():
    args = sys.argv[1:]
    ram_go = 4.0                                     # limite DuckDB : laisse de la marge au reste du poste (16 Go)
    if "--ram-go" in args:
        i = args.index("--ram-go")
        ram_go = float(args[i + 1])
        del args[i:i + 2]
    if not args:
        sys.exit(__doc__)
    pbf = Path(args[0]).resolve()
    sortie = Path(args[1]).resolve() if len(args) > 1 else pbf.with_suffix("").with_suffix(".parquet")
    # Temporaires sur le même disque que le PBF (D:), pas dans %TEMP% (C:)
    tmp = pbf.parent / "tmp"
    tmp.mkdir(exist_ok=True)
    for var in ("TMP", "TEMP", "TMPDIR"):
        os.environ[var] = str(tmp)
    tempfile.tempdir = str(tmp)
    debut = time.perf_counter()
    with SondeRam() as sonde:
        # keep_all_tags : on garde tous les tags (cycleway:*, oneway:bicycle, surface…) dans une colonne map
        quackosm.convert_pbf_to_parquet(
            pbf,
            tags_filter=FILTRE_TAGS,
            keep_all_tags=True,
            result_file_path=sortie,
            working_directory=tmp,
            ignore_cache=True,
            memory_limit=int(ram_go * 1024 ** 3),    # en octets ; DuckDB déborde sur disque au-delà
            # extension spatial installée sur D: (cache/duckdb_ext) et non dans ~/.duckdb (C:)
            duckdb_conn_kwargs={"config_kwargs": {"extension_directory": str(Path(__file__).resolve().parent / "cache" / "duckdb_ext")}},
        )
    duree = time.perf_counter() - debut
    mo = 1024 ** 2
    print(f"PBF        : {pbf.stat().st_size / mo:,.0f} Mo")
    print(f"GeoParquet : {sortie.stat().st_size / mo:,.0f} Mo ({sortie})")
    print(f"Durée      : {duree / 60:.1f} min")
    print(f"RAM max    : {sonde.max_octets / mo:,.0f} Mo (limite DuckDB {ram_go:g} Go)")
    print(f"RAM poste  : {psutil.virtual_memory().total / mo:,.0f} Mo")


if __name__ == "__main__":
    main()

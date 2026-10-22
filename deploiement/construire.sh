#!/usr/bin/env bash
# Construit dist/ : ce que Cloudflare Workers publie (ressources statiques seules, spec §4).
# La page, le classement, coeur.js et les tuiles par territoire (tuiles/fr, tuiles/97x). Les anciennes tuiles à la racine de
# tuiles/ et france.html restent sur GitHub Pages jusqu'à la phase 4 (métropole).
# Limites de l'offre gratuite : 20 000 fichiers par version, 25 Mio par fichier (vérifiées ici).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf dist && mkdir -p dist/tuiles
cp index.html classement.html coeur.js tuiles-worker.js osm-contrib.js LICENSE dist/
cp deploiement/_headers dist/
for d in tuiles/*/; do cp -r "$d" dist/tuiles/; done
# Classement des départements (classement.html) : contours des 96 départements et scores (chaine/scores.js)
cp tuiles/departements.json dist/tuiles/
[ -f tuiles/classement.json ] && cp tuiles/classement.json dist/tuiles/ || echo "tuiles/classement.json absent : classement.html sans données"
rm -f dist/tuiles/*/progression.json
n=$(find dist -type f | wc -l)
gros=$(find dist -type f -size +25M)
echo "dist/ : $n fichiers, $(du -sh dist | cut -f1)"
[ "$n" -le 20000 ] || { echo "plus de 20 000 fichiers : regrouper les tuiles (spec §4)"; exit 1; }
[ -z "$gros" ] || { echo "fichiers de plus de 25 Mio : $gros"; exit 1; }

#!/usr/bin/env bash
# Sondage des sources de données depuis GitHub Actions (le conteneur de travail n'atteint pas ces services).
# 1-3 (16-17/10) : BPE, Filosofi ; 4 : communes et EPCI (geo.api.gouv.fr). 5 (17/10) : position des mairies des
# 35 000 communes (champ mairie de geo.api.gouv.fr ?), pour les scores par commune.
set -uo pipefail
api=https://geo.api.gouv.fr
for f in "code,nom,mairie" "code,nom,centre" "code,nom,mairie,centre,population,codeDepartement"; do
  echo "=== /communes?codeDepartement=17&fields=$f"
  curl -sS --max-time 120 "$api/communes?codeDepartement=17&fields=$f" | jq -c 'length, .[0:2]'
done
echo "=== France : communes avec mairie"
curl -sS --max-time 300 "$api/communes?fields=code,mairie,centre" -o /tmp/m.json
jq -c 'length, ([.[] | select(.mairie == null)] | length), ([.[] | select(.centre == null)] | length)' /tmp/m.json
jq -c '[.[] | select(.mairie == null)] | .[0:5]' /tmp/m.json

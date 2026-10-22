# À portée de vélo — Charente-Maritime : symbologie et analyse (dossier de travail)

Dossier de travail sur la représentation du **confort** des trajets à vélo, à partir des tuiles OSM de la Charente-Maritime (et 25 km autour) : temps de trajet réels, part des trajets faite sur aménagement cyclable, maillons manquants.

La version **France entière** (choroplèthe des 96 chefs-lieux, scores, site publié) est en pause dans un dossier à part : `D:/Documents/Projets/ia/perso/portee-velo-france`, publiée sur https://aurelienchaumet.github.io/portee-velo-france/. Ce dossier-ci n'est relié à aucun dépôt distant.

La méthode (chaîne PBF → tuiles, format, calcul dans le navigateur) est décrite dans le README de la version France ; elle n'a pas changé.

## Maquette de symbologie

Bouton « Temps / Temps et confort » (lien : `#v=confort`, ou l'ancien `?maquette`) : carte bivariée, clarté = temps de trajet (5 à 60 min), teinte = confort (bleu : trajet aménagé ; gris : mitigé ; orange : surtout sur route ; rampe sûre pour les daltoniens). Traits noirs : tronçons de route sans aménagement par lesquels passent les trajets les plus rapides vers au moins 10 / 25 / 60 km². Vitesses réelles, +5 % sur route pour départager une piste parallèle.

Classes des tuiles : 0 piste, 1 bande, 2 rues et voies de service, 3 secondaire, 4 principale, 5 voie communale (unclassified), 6 chemin (track, path), 7 départementale tertiaire. Le regroupement en confort se fait dans la page (`CONFORT`), sans re-tuiler.

- Par défaut (option A) : exposé = tertiaire, très exposé = secondaire et principale ; rues, voies communales et chemins = calmes.

Tester : `python -m http.server 8765` puis http://localhost:8765/

## Licence

Code : [MIT](LICENSE), © Aurélien Chaumet, avec des portions © PETR Pôle Marennes-Oléron (MIT). Données dérivées d'OpenStreetMap (`tuiles/`, `test/fixtures/`) : ODbL, © contributeurs OpenStreetMap.

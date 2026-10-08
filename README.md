# À portée de vélo – Marennes-Oléron

Carte interactive des temps de trajet à vélo sur l'île d'Oléron et le bassin de Marennes.

👉 **https://sit-pmo.github.io/portee-velo-oleron/**

Déplacez la souris sur la carte : les temps de trajet depuis le curseur se recalculent en direct. Cliquez pour fixer le départ, puis cliquez ailleurs pour obtenir l'itinéraire, sa durée et sa part sur aménagement cyclable. Sur mobile, faites glisser le repère D.

## Objectif

Rendre visible, en un coup d'œil, ce que le réseau cyclable permet réellement d'atteindre à vélo, et où il manque des liaisons.

Deux modes sont proposés :

- **Pistes et bandes** : uniquement le réseau aménagé (pistes, voies vertes, bandes). Les zones grises sont à plus de 800 m de ce réseau : elles montrent où il manque des aménagements.
- **Pistes + routes** : le réseau aménagé complété par les routes et chemins, avec un temps majoré selon le type de voie.

## Ce qui a été fait

### Le territoire

14 communes : les 8 communes de la CdC de l'Île d'Oléron et les 6 communes de la CC du Bassin de Marennes (Bourcefranc-le-Chapus, Le Gua, Marennes-Hiers-Brouage, Nieulle-sur-Seudre, Saint-Just-Luzac, Saint-Sornin), soit 360 km². Le calcul déborde de 3 km sur les communes voisines, pour ne pas couper les itinéraires en limite. Les pourcentages de surface atteinte portent sur les 14 communes.

### Un réseau cyclable consolidé

Le réseau combine trois sources, avec une règle simple : **la donnée des collectivités fait foi là où elle existe**.

1. **Réseau cyclable de la CdC de l'Île d'Oléron** (183 km, dont 170 km existants).
2. **Réseau cyclable de la CC du Bassin de Marennes** (75 km).
3. **OpenStreetMap**, uniquement pour les aménagements absents des bases des collectivités (environ 13 km, surtout des bandes cyclables communales en agglomération). Ces compléments sont vérifiés un par un dans QGIS avant intégration (fait pour l'île, en cours pour le bassin de Marennes).

Les tronçons en projet ou à l'étude sont exclus : la carte montre le réseau tel qu'il existe aujourd'hui.

Les routes et chemins viennent d'OpenStreetMap. Le réseau consolidé y est raccordé automatiquement : un nœud est créé à chaque croisement entre une piste et une voie, et les extrémités de piste sont reliées à la voie la plus proche quand elle est à moins de 15 m.

### Le modèle de calcul

| Élément | Hypothèse |
|---|---|
| Vitesse sur revêtement lisse (enrobé, béton) | 20 km/h |
| Vitesse sur stabilisé, grave, bois | 13 à 17 km/h |
| Bande cyclable | temps majoré de 10 % |
| Route ou chemin partagé | temps majoré de 50 % |
| Route secondaire | temps doublé |
| Route principale sans aménagement | temps triplé |
| Sens unique pris à contre-sens | vélo poussé, à 5 km/h |
| Accès au réseau depuis un point quelconque | à vol d'oiseau, majoré de 30 %, à 16 km/h |

Ni vent ni relief. Le territoire est découpé en cellules de 100 m ; pour chaque départ, un algorithme de plus court chemin (Dijkstra) calcule le temps vers toutes les voies, puis vers chaque cellule. Tout le calcul se fait dans le navigateur, en 40 à 70 ms : c'est ce qui permet le suivi en direct de la souris.

Deux choix ont été faits pour qu'aucune zone n'apparaisse coupée à tort en mode « Pistes + routes » : les routes principales restent franchissables (temps triplé plutôt qu'interdites), et les sens uniques restent praticables à pied.

## Comment

La page est statique : un fichier `index.html` (Leaflet et d3 pour la carte, fond Plan IGN de la Géoplateforme, recherche d'adresses Géoplateforme) et un fichier de données précalculées `data/oleron_velo.js` (graphe du réseau et grille). Pas de serveur, pas de base de données : GitHub Pages sert les deux fichiers.

Les données sont préparées en amont par le SIT avec des scripts Python (numpy, shapely, pyproj), à partir des bases cyclables des collectivités et d'OpenStreetMap :

1. extraction des aménagements OpenStreetMap et repérage de ceux absents des bases des collectivités ;
2. vérification de ces compléments dans QGIS ;
3. fusion en un réseau consolidé ;
4. construction du graphe (réseau consolidé + routes OSM, raccordements, contrôle des extrémités non raccordées) et de la grille, puis export de `data/oleron_velo.js`.

Pour mettre la carte à jour, le SIT régénère `data/oleron_velo.js` et le pousse sur ce dépôt : le site se met à jour en une à deux minutes. Seuls les fichiers nécessaires à l'affichage sont publiés ici.

## Sources et licences

- Réseaux cyclables : CdC de l'Île d'Oléron, CC du Bassin de Marennes.
- Routes, chemins et compléments cyclables : © contributeurs [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL).
- Contours communaux : [geo.api.gouv.fr](https://geo.api.gouv.fr).
- Fond de carte et recherche d'adresses : [Géoplateforme IGN](https://geoservices.ign.fr).
- Bibliothèques : [Leaflet](https://leafletjs.com), [d3](https://d3js.org).

Les données dérivées d'OpenStreetMap contenues dans `data/oleron_velo.js` sont diffusées sous ODbL.

## Inspiration

Cette carte reprend le principe d'[À portée de tram](https://tram.camilleroux.com/) de Camille Roux, lui-même issu du [NYC Transit Time Cartogram](https://castrio.me/nyc/) d'Anthony Castrio et de sa [déclinaison parisienne](https://github.com/JulesGrandin/paris-temps-transport) par Jules Grandin. Le code et les calculs ont été écrits pour ce projet : le réseau est ici cyclable, et non de transport en commun.

## Réalisation

Système d'Information Territoriale (SIT) du Pôle Marennes-Oléron.

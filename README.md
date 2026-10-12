# À portée de vélo – Charente-Maritime

Carte interactive des temps de trajet à vélo en Charente-Maritime, et du confort de ces trajets.

👉 **https://sit-pmo.github.io/portee-velo/**

Vues limitées à un territoire (seul le territoire est coloré, pourcentages de surface atteinte) :

- île d'Oléron : **https://sit-pmo.github.io/portee-velo/?territoire=oleron**
- Pôle Marennes-Oléron (île d'Oléron et bassin de Marennes, 14 communes) : **https://sit-pmo.github.io/portee-velo/?territoire=pmo**

Déplacez la souris sur la carte : les temps de trajet depuis le curseur se recalculent en direct. Cliquez pour fixer le départ, puis cliquez ailleurs pour obtenir l'itinéraire, sa durée et sa part sur aménagement cyclable. Sur mobile, faites glisser le repère D.

## Objectif

Rendre visible, en un coup d'œil, ce que le réseau cyclable permet réellement d'atteindre à vélo, dans quel confort, et où il manque des liaisons.

Deux réseaux :

- **Pistes et bandes** : le réseau aménagé (pistes, voies vertes, bandes), avec de courtes liaisons entre deux pistes (jusqu'à 200 m sur rue calme ou chemin, 100 m sur route) et les traversées de chaussée. En gris, les secteurs à plus de 800 m de ce réseau.
- **Pistes + routes** : le réseau aménagé complété par les routes et chemins.

Deux lectures en mode « Pistes + routes » :

- **Temps** : temps du trajet le plus rapide, de 5 à 60 minutes.
- **Temps et confort** : la clarté donne le même temps ; la teinte dit ce que coûte un trajet **tranquille** (qui préfère pistes, bandes, rues calmes et chemins, chaque minute restante sur route exposée comptant pour 4) par rapport au plus rapide : bleu = moins de 20 % de plus, gris = 20 à 50 %, orange = plus de 50 %. La couleur décrit le trajet depuis le départ, pas l'équipement du lieu : une piste qu'on ne rejoint que par la route reste orange. Les **traits noirs** marquent les tronçons de route par lesquels passent les trajets les plus rapides vers les secteurs orange : les maillons manquants.

Classement des voies : **calmes** = rues, voies de service, voies communales, chemins ; **exposées** = routes départementales tertiaires ; **très exposées** = routes secondaires et principales.

## Simuler un aménagement

Pour mesurer l'effet d'une future piste avant de la financer, par exemple sur un maillon manquant :

- **Tracer un tronçon** : cliquez les sommets de la piste, double-clic (ou Entrée) pour terminer. La piste est raccordée à chaque voie qu'elle croise, et à la voie la plus proche de chaque sommet posé à moins de 25 m (point plein ; cercle creux sinon). Un tronçon qui ne croise rien et dont aucun sommet n'est raccordé reste sans effet.
- **Importer** un GeoJSON de lignes, en WGS84 ou en Lambert 93 (reconnu automatiquement), par exemple un projet dessiné dans QGIS ; **Exporter** pour reprendre les tracés ailleurs.
- **Aménager une voie** : chaque clic sur une route la transforme en piste jusqu'aux deux carrefours les plus proches ; cliquez la suite pour la prolonger, Échap pour arrêter.
- **Modifier** : cliquez un tronçon, puis faites glisser ses sommets ; cliquez sur son trait (ou sur une poignée de milieu) pour ajouter un sommet, clic droit pour en supprimer un.

La piste simulée compte comme une piste existante (20 km/h, double sens). La carte montre l'**Après** ; **Avant** revient à l'existant ; **Gain** ne colore que les secteurs améliorés : minutes gagnées en « Temps », passage au bleu en « Temps et confort » (une piste le long d'une route change surtout le confort). Le bandeau chiffre le gain en km² atteints (en points de % dans les vues territoire) et en km² passés au bleu.

Les tronçons voyagent dans le lien « Copier le lien de cette vue » (jusqu'à quelques centaines de sommets ; au-delà, exportez-les en GeoJSON) et sont gardés dans votre navigateur d'une visite à l'autre.

## Maillons manquants de l'île (vue Oléron)

Avec `?territoire=oleron`, le bloc « Maillons manquants de l'île » classe les voies qui coupent le plus souvent l'accès tranquille. Pour chaque départ d'une grille régulière de 250 m couvrant l'île, on relève les traits noirs (routes exposées par lesquelles passent les trajets les plus rapides vers au moins 10 km² en orange), puis on compte, voie par voie, les départs concernés. Les morceaux consécutifs de même classe qui se suivent sans autre voie classée au carrefour sont regroupés (score du meilleur morceau). « Vue cumulée » les dessine (épaisseur selon la part des départs), un clic dans la liste zoome sur la voie, « Aménager et voir le gain » la simule en piste depuis le départ où elle pèse le plus.

Le classement est précalculé (`departement17/maillons.js`, environ 3 minutes) et doit être relancé après chaque tuilage.

## Équipements et habitants à portée

Si `tuiles/accessibilite.json` est présent, le bloc « À portée depuis ce départ » compte ce qui tombe dans les cellules atteintes à 15 ou 30 min : habitants et résidences principales (Filosofi carroyé), emplacements de camping ou toute autre couche de points pondérés, et équipements, services et commerces de la Base permanente des équipements (BPE) par domaine. Trois colonnes, quel que soit le réseau choisi en haut :

- **Pistes seules** : le réseau aménagé, avec ses courtes liaisons ;
- **Tranquille** : routes comprises, mais seulement les cellules en bleu (éviter la route exposée y coûte moins de 20 % de temps en plus) ;
- **Tous trajets** : routes comprises.

En vue territoire, seul le territoire compte et chaque valeur est rapportée à son total. Le **panier du quotidien** (alimentation, boulangerie, pharmacie, médecin généraliste, école, poste) est compté selon les trois mesures ; chaque élément est coloré par la meilleure qui l'atteint. « Montrer les équipements » les place sur la carte (plein = par les pistes ou tranquillement, pâle = par la route seulement, creux = au-delà). Avec des tronçons simulés, le gain s'affiche en violet.

Le fichier est préparé par `departement17/comptages.py` (BPE avec son fichier de modalités, Filosofi, points pondérés), à relancer après chaque tuilage. `departement17/quart_heure.js` calcule, pour chaque cellule habitée d'un territoire et selon les trois mesures, les éléments du panier atteints, et la part des habitants qui l'ont en entier à 15 min (synthèse JSON et GeoJSON pour QGIS).

## Données

- **Réseau cyclable des collectivités** sur les 14 communes du Pôle Marennes-Oléron : CdC de l'Île d'Oléron (183 km) et CC du Bassin de Marennes (75 km), complétés par les aménagements OpenStreetMap absents de ces bases, vérifiés un par un dans QGIS. Là, la donnée des collectivités fait foi et remplace les pistes OpenStreetMap ; elle est raccordée aux routes à chaque croisement et aux extrémités (voie la plus proche à moins de 25 m). Les tronçons en projet sont exclus.
- **OpenStreetMap** partout ailleurs, et pour les routes et chemins sur tout le département. La qualité du réseau cyclable dépend donc de la cartographie OpenStreetMap locale.

## Modèle de calcul

| Élément | Hypothèse |
|---|---|
| Vitesse sur revêtement lisse (enrobé, béton) | 20 km/h, sur piste comme sur route |
| Vitesse sur stabilisé, grave, bois | 13 à 17 km/h |
| Vitesse sur sable | 8 km/h |
| Route | +5 % seulement, pour préférer une piste qui la longe |
| Sens unique pris à contre-sens | vélo poussé, à 5 km/h |
| Accès au réseau depuis un point quelconque | à vol d'oiseau, majoré de 30 %, à 16 km/h |

Ni vent ni relief. Le territoire est découpé en cellules de 100 m ; pour chaque départ, un algorithme de plus court chemin (Dijkstra) calcule le temps vers toutes les voies, puis vers chaque cellule. Tout le calcul se fait dans le navigateur, en quelques dizaines de millisecondes : c'est ce qui permet le suivi en direct de la souris.

## Comment

La page est statique : `index.html` (Leaflet et d3, fond Plan IGN de la Géoplateforme, recherche d'adresses Géoplateforme), `coeur.js` (le calcul) et le dossier `tuiles/` : le graphe du réseau et la grille, découpés en 380 tuiles d'environ 10 km (22 Mo en tout). La page ne charge que les 5 × 5 tuiles autour du départ. Pas de serveur, pas de base de données : GitHub Pages sert les fichiers.

Publication : à chaque push sur `main`, un workflow copie les fichiers publiés sur la branche `gh-pages`, que sert GitHub Pages. Chaque pull request a son aperçu, publié sous `https://sit-pmo.github.io/portee-velo/pr-preview/pr-<numéro>/` (lien en commentaire de la PR) et retiré à sa fermeture.

Les tuiles sont préparées en amont par le SIT avec des scripts Python (DuckDB, numpy, shapely, pyproj), à partir des extraits OpenStreetMap de Geofabrik (Poitou-Charentes, Aquitaine, Pays de la Loire) et du réseau consolidé des collectivités. Seuls les fichiers nécessaires à l'affichage sont publiés ici.

## Sources et licences

- Réseaux cyclables : CdC de l'Île d'Oléron, CC du Bassin de Marennes.
- Routes, chemins, réseau cyclable hors Marennes-Oléron, trait de côte : © contributeurs [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL), extraits [Geofabrik](https://download.geofabrik.de).
- Contours communaux : [geo.api.gouv.fr](https://geo.api.gouv.fr).
- Équipements : Insee, Base permanente des équipements. Habitants et ménages : Insee, Filosofi carroyé.
- Fond de carte et recherche d'adresses : [Géoplateforme IGN](https://geoservices.ign.fr).
- Bibliothèques : [Leaflet](https://leafletjs.com), [d3](https://d3js.org).

Les données dérivées d'OpenStreetMap contenues dans `tuiles/` sont diffusées sous ODbL.

## Licence

Le code (`index.html`, `coeur.js`) est diffusé sous [licence MIT](LICENSE), © PETR Pôle Marennes-Oléron et © Aurélien Chaumet. Les données ne sont pas couvertes par cette licence : voir ci-dessus.

## Inspiration

Cette carte reprend le principe d'[À portée de tram](https://tram.camilleroux.com/) de Camille Roux, lui-même issu du [NYC Transit Time Cartogram](https://castrio.me/nyc/) d'Anthony Castrio et de sa [déclinaison parisienne](https://github.com/JulesGrandin/paris-temps-transport) par Jules Grandin. Le code et les calculs ont été écrits pour ce projet : le réseau est ici cyclable, et non de transport en commun.

## Réalisation

Système d'Information Territoriale (SIT) du Pôle Marennes-Oléron.

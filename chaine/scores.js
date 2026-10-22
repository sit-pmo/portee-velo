// Scores des 101 départements : surface de terre (km²) atteinte à vélo en 15 min depuis la mairie du chef-lieu,
// avec les routes (« tout ») et sans (« amenage »). Même code que la page (coeur.js), mêmes tuiles : un départ
// posé sur la mairie dans index.html affiche exactement ces chiffres.
//
// Métropole : chefs-lieux de chefs_lieux.json, tuiles/fr. DROM : départ de tuiles/97x/contour.json (mairie de la
// commune la plus peuplée : le chef-lieu, sauf en Guadeloupe où c'est Les Abymes et non Basse-Terre).
//
// Usage (depuis la racine du dépôt, tuiles présentes) : node chaine/scores.js [dossier des tuiles]
//   → <dossier>/classement.json (lancé par deployer.yml après la récupération des tuiles)
const fs = require("fs");
const path = require("path");
const C = require("../coeur.js");

const DOSSIER = process.argv[2] || path.join(__dirname, "..", "tuiles");
const MINUTES = 15;
// Fenêtre 3 × 3 tuiles : 15 min à 20 km/h = 5 km (+ 2 km d'accès au plus), sous la marge garantie de 10 km
const RAYON_TUILES = 1;
const DROM = ["971", "972", "973", "974", "976"];

const lireJson = f => JSON.parse(fs.readFileSync(f, "utf8"));

// Surfaces atteintes depuis une liste de départs sur un territoire (une grille, un dossier de tuiles)
async function scoresTerritoire(zone, departs) {
  const d = path.join(DOSSIER, zone);
  const index = lireJson(path.join(d, "tuiles.json"));
  C.definirGrille(index.grille);
  const chargeur = new C.ChargeurTuiles({ index, rayon: RAYON_TUILES, capacite: 20,
    lire: (tx, ty) => fs.readFileSync(path.join(d, `${tx}_${ty}.bin`)) });
  const lignes = [];
  for (const c of departs) {
    const f = await chargeur.fenetre(c.lon, c.lat);
    const ligne = { zone, dep: c.dep, departement: c.departement, commune: c.commune, lon: c.lon, lat: c.lat };
    for (const mode of ["tout", "amenage"]) {
      const G = f.P ? C.construireGraphe(f.P, mode) : null;
      const acc = G && C.accrocher(f.P, G, c.lon, c.lat);
      if (!acc) { ligne[mode] = { km2: 0, accesM: null }; continue; }   // aucune voie du mode à portée d'accès
      const res = C.dijkstra(G, acc.noeud, C.accesSecondes(acc.distM), MINUTES * 60);
      const [km2] = C.surfaceKm2(f.P, C.tempsGrille(f.P, G, res), [MINUTES]);
      ligne[mode] = { km2: Math.round(km2 * 10) / 10, accesM: Math.round(acc.distM) };
    }
    lignes.push(ligne);
    console.log(`${c.dep.padStart(3)} ${c.commune.padEnd(26)} ${String(ligne.tout.km2).padStart(6)} km² | pistes ${String(ligne.amenage.km2).padStart(5)} km²`);
  }
  return { lignes, genere: index.genere };
}

async function main() {
  const chefs = lireJson(path.join(__dirname, "chefs_lieux.json"));
  const fr = await scoresTerritoire("fr", chefs);
  const departements = [...fr.lignes];
  for (const z of DROM) {
    if (!fs.existsSync(path.join(DOSSIER, z, "tuiles.json"))) { console.log(`${z} : tuiles absentes`); continue; }
    const c = lireJson(path.join(DOSSIER, z, "contour.json"));
    if (!c.depart) { console.log(`${z} : pas de départ dans contour.json`); continue; }
    departements.push(...(await scoresTerritoire(z, [{ dep: z, departement: c.nom, commune: c.depart.commune,
      lon: c.depart.lon, lat: c.depart.lat }])).lignes);
  }
  const sortie = { minutes: MINUTES, genere: new Date().toISOString().slice(0, 16), tuiles: fr.genere, departements };
  fs.writeFileSync(path.join(DOSSIER, "classement.json"), JSON.stringify(sortie));
  const tri = [...departements].sort((a, b) => b.tout.km2 - a.tout.km2);
  console.log(`\nÉcrit : ${path.join(DOSSIER, "classement.json")} (${departements.length} départements)`);
  console.log("Tête :", tri.slice(0, 5).map(d => `${d.commune} ${d.tout.km2}`).join(", "));
}

main().catch(e => { console.error(e); process.exit(1); });

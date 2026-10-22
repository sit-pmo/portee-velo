// Scores des ~35 000 communes (17/10/2026, TODOS « Étendre aux 35 000 communes ») : surface de terre (km²) atteinte à
// vélo en 15 min depuis la mairie de chaque commune, avec les routes (« tout ») et sans (« amenage »). Même calcul que
// scores.js (chefs-lieux) et que la page : fenêtre de 3 × 3 tuiles, Dijkstra borné, coeur.js.
// Mairies : champ « mairie » de geo.api.gouv.fr (toutes les communes en ont une), téléchargé par diagnostiquer.yml.
//
// Deux étapes, la première parallélisable :
//   node scores_communes.js calculer --communes cache/geo/communes_mairies.json --tuiles ../tuiles --part 0/4
//        → cache/scores/part_0.json
//   node scores_communes.js assembler --communes cache/geo/communes_mairies.json --sortie <dossier>
//        → <dossier>/communes.json (colonnes : code, nom, dep, lon, lat, pop, tout, amenage), lu par classement.html
"use strict";
const fs = require("fs");
const path = require("path");
const C = require("../coeur.js");

const MINUTES = 15;
const RAYON_TUILES = 1;                   // 15 min à 20 km/h = 5 km (+ 2 km d'accès au plus) : sous la marge de 10 km
const PARTS = path.join(__dirname, "cache", "scores");

function args() {
  const a = { cmd: process.argv[2] };
  for (let i = 3; i < process.argv.length; i += 2) a[process.argv[i].replace(/^--/, "")] = process.argv[i + 1];
  return a;
}
const lireJSON = f => JSON.parse(fs.readFileSync(f, "utf8"));
// Territoire d'une commune : DROM d'après le code INSEE (971…976), métropole sinon ; collectivités d'outre-mer
// (975, 977, 978, et 984 à 989 : TAAF, Wallis-et-Futuna, Polynésie, Nouvelle-Calédonie, Clipperton) sans tuiles
const zoneDe = code => (/^97[1-46]/.test(code) ? code.slice(0, 3) : /^9[78]/.test(code) ? null : "fr");

// Communes d'un territoire, triées par tuile (ty, tx) : les fenêtres successives se recouvrent, le cache sert
function parTuile(liste, index) {
  const g = index.grille, T = g.cellulesTuile;
  const t = c => [Math.floor((g.lat0 - c.lat) / (g.dlat * T)), Math.floor((c.lon - g.lon0) / (g.dlon * T))];
  return liste.map(c => ({ ...c, t: t(c) })).sort((a, b) => a.t[0] - b.t[0] || a.t[1] - b.t[1]);
}

async function calculer(a) {
  const toutes = lireJSON(a.communes).filter(c => c.mairie && zoneDe(c.code))
    .map(c => ({ code: c.code, lon: c.mairie.coordinates[0], lat: c.mairie.coordinates[1], zone: zoneDe(c.code) }));
  const [ip, np] = (a.part || "0/1").split("/").map(Number);
  const resultats = [];
  const t0 = Date.now();
  for (const zone of [...new Set(toutes.map(c => c.zone))].sort()) {
    const d = path.join(a.tuiles, zone);
    if (!fs.existsSync(path.join(d, "tuiles.json"))) { console.log(`${zone} : tuiles absentes`); continue; }
    const index = lireJSON(path.join(d, "tuiles.json"));
    C.definirGrille(index.grille);
    // Part ip : une tranche contiguë des communes triées par tuile (et non une sur np : le cache sert mieux)
    const liste = parTuile(toutes.filter(c => c.zone === zone), index);
    const n = liste.length, tranche = liste.slice(Math.floor(n * ip / np), Math.floor(n * (ip + 1) / np));
    const chargeur = new C.ChargeurTuiles({ index, rayon: RAYON_TUILES, capacite: 30,
      lire: (tx, ty) => fs.readFileSync(path.join(d, `${tx}_${ty}.bin`)) });
    let cle = null, P = null, G = null;
    for (const [i, c] of tranche.entries()) {
      const k = c.t.join("_");
      if (k !== cle) {                    // nouvelle tuile centrale : fenêtre et graphes des deux réseaux
        const f = await chargeur.fenetre(c.lon, c.lat);
        cle = k; P = f.P;
        G = P ? { tout: C.construireGraphe(P, "tout"), amenage: C.construireGraphe(P, "amenage") } : null;
      }
      const r = { code: c.code };
      for (const mode of ["tout", "amenage"]) {
        const acc = G && C.accrocher(P, G[mode], c.lon, c.lat);
        if (!acc) { r[mode] = 0; continue; }   // aucune voie du réseau à portée d'accès de la mairie
        const res = C.dijkstra(G[mode], acc.noeud, C.accesSecondes(acc.distM), MINUTES * 60);
        r[mode] = Math.round(C.surfaceKm2(P, C.tempsGrille(P, G[mode], res), [MINUTES])[0] * 10) / 10;
      }
      resultats.push(r);
      if (i % 2000 === 0) console.log(`${zone} ${i + 1}/${tranche.length} (${Math.round((Date.now() - t0) / 1000)} s)`);
    }
  }
  fs.mkdirSync(PARTS, { recursive: true });
  const f = path.join(PARTS, `part_${ip}.json`);
  fs.writeFileSync(f, JSON.stringify(resultats));
  console.log(`→ ${f} : ${resultats.length} communes en ${Math.round((Date.now() - t0) / 1000)} s`);
}

function assembler(a) {
  const scores = new Map();
  for (const f of fs.readdirSync(PARTS).filter(f => /^part_\d+\.json$/.test(f)))
    for (const r of lireJSON(path.join(PARTS, f))) scores.set(r.code, r);
  const liste = lireJSON(a.communes).filter(c => scores.has(c.code) && zoneDe(c.code)).sort((x, y) => x.code.localeCompare(y.code));
  const col = { code: [], nom: [], dep: [], lon: [], lat: [], pop: [], tout: [], amenage: [] };
  for (const c of liste) {
    const s = scores.get(c.code);
    col.code.push(c.code); col.nom.push(c.nom); col.dep.push(c.codeDepartement);
    col.lon.push(c.mairie.coordinates[0]); col.lat.push(c.mairie.coordinates[1]); col.pop.push(c.population || 0);
    col.tout.push(s.tout); col.amenage.push(s.amenage);
  }
  fs.mkdirSync(a.sortie, { recursive: true });
  const sortie = path.join(a.sortie, "communes.json");
  fs.writeFileSync(sortie, JSON.stringify({ minutes: MINUTES, genere: new Date().toISOString().slice(0, 16),
    source: "Mairies : geo.api.gouv.fr ; réseau : OpenStreetMap", communes: col }));
  const nuls = col.amenage.filter(v => v === 0).length;
  console.log(`→ ${sortie} : ${liste.length} communes ; sans les routes, ${nuls} sans piste à portée de la mairie`);
}

const a = args();
if (a.cmd === "calculer") calculer(a).catch(e => { console.error(e); process.exit(1); });
else if (a.cmd === "assembler") assembler(a);
else { console.error("usage : node scores_communes.js calculer|assembler --communes … [--tuiles …] [--part i/N] [--sortie …]"); process.exit(2); }

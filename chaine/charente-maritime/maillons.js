// maillons.js : classement des maillons manquants d'un territoire (D4, 12/10/2026).
// Départs sur une grille régulière (sans pondération), traits noirs de chaque départ (règle de la page), cumulés par
// voie entre carrefours. Seule la fenêtre fixe du territoire est chargée (RAM du poste).
// Usage (depuis chaine/charente-maritime/) :
//   node maillons.js --territoire oleron --pas 250 --comparer 500 --sortie ../../marennes-oleron/tuiles/maillons_oleron.json
const fs = require("fs");
const path = require("path");
const C = require("../../marennes-oleron/coeur.js");

const arg = (nom, defaut) => { const i = process.argv.indexOf(`--${nom}`); return i > 0 ? process.argv[i + 1] : defaut; };
const TERRITOIRE = arg("territoire", "oleron"), PAS = Number(arg("pas", 250)), COMPARER = arg("comparer", null);
const SORTIE = arg("sortie", null), DOSSIER = arg("tuiles", path.join(__dirname, "..", "tuiles"));
const SEUIL_PART = 0.02;           // voies publiées : trait noir depuis au moins 2 % des départs comptés

(async () => {
  const index = JSON.parse(fs.readFileSync(path.join(DOSSIER, "tuiles.json"), "utf8"));
  const terr = JSON.parse(fs.readFileSync(path.join(DOSSIER, "territoires.json"), "utf8"))[TERRITOIRE];
  if (!terr) throw new Error(`territoire inconnu : ${TERRITOIRE}`);
  const z = C.decoderMasque(terr.masque, index.grille);
  // Même fenêtre que la page en vue territoire (5 × 5 tuiles autour du centre du masque)
  const chargeur = new C.ChargeurTuiles({ index, lire: (tx, ty) => fs.promises.readFile(path.join(DOSSIER, `${tx}_${ty}.bin`)) });
  const f = await chargeur.fenetre(...z.centre);
  if (!f.P) throw new Error("fenêtre vide");
  const P = f.P, g = P.grille;
  g.pro.confort = g.pro.tout; g.dist.confort = g.dist.tout;      // comme la page : même réseau que « tout »
  C.appliquerMasque(g, z, index.grille);
  const G = { tout: C.construireGraphe(P, "tout"), confort: C.construireGraphe(P, "confort") };
  console.log(`fenêtre : ${P.n} nœuds, ${P.m} arêtes${f.partielle ? " (partielle !)" : ""}`);

  // Un calcul complet au pas donné, avec durée
  const calculer = pas => {
    const pts = C.grilleDeparts(g, pas), t0 = Date.now();
    const r = C.cumulerMaillons(P, G, pts);
    console.log(`pas ${pas} m : ${r.departs} départs (${r.ignores} ignorés), ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    return r;
  };
  // Voies classées : score ≥ SEUIL_PART des départs comptés, par score décroissant
  const classees = r => {
    const min = Math.max(1, Math.ceil(SEUIL_PART * (r.departs - r.ignores)));
    const ids = [];
    r.score.forEach((s, v) => { if (s >= min) ids.push(v); });
    return ids.sort((a, b) => r.score[b] - r.score[a]);
  };

  // Voies classées qui se suivent (même classe, nœud partagé par exactement deux voies classées) fusionnées : une
  // départementale coupée par des chemins d'accès ne fait qu'une ligne (sinon la tête de liste alignait 27 m, 351 m,
  // 103 m… de la même route). ids triés par score décroissant : chaque chaîne part de son meilleur morceau, dont elle
  // garde le score et le départ représentatif. Rend [{ v (meilleur morceau), noeuds }].
  const fusionner = (r, ids) => {
    const bouts = new Map(), fait = new Set(), out = [];
    for (const v of ids) for (const n of [r.liste[v][0], r.liste[v][r.liste[v].length - 1]]) {
      if (!bouts.has(n)) bouts.set(n, []);
      bouts.get(n).push(v);
    }
    for (const v0 of ids) {
      if (fait.has(v0)) continue;
      fait.add(v0);
      let noeuds = [...r.liste[v0]];
      const cls = dominante[v0];
      for (const finale of [true, false]) {          // prolonger par la fin, puis par le début
        for (;;) {
          const n = finale ? noeuds[noeuds.length - 1] : noeuds[0], l = bouts.get(n);
          if (l.length !== 2) break;
          const w = l[0] === l[1] ? -1 : l.find(x => !fait.has(x));
          if (w === undefined || w < 0 || dominante[w] !== cls) break;
          fait.add(w);
          let suite = r.liste[w];
          if (finale) { if (suite[0] !== n) suite = [...suite].reverse(); noeuds = [...noeuds, ...suite.slice(1)]; }
          else { if (suite[suite.length - 1] !== n) suite = [...suite].reverse(); noeuds = [...suite.slice(0, -1), ...noeuds]; }
        }
      }
      out.push({ v: v0, noeuds });
    }
    return out;
  };

  const r = calculer(PAS), ids = classees(r);
  // Classe d'une voie = sa classe la plus exposée (CFG.confort.classes) : une voie ne coupe pas au changement de
  // classe, une départementale prolongée par une rue doit s'afficher « départementale », et la fusion compare celle-ci
  const expo = c => C.CFG.confort.classes[c], dominante = new Int32Array(r.liste.length).fill(-1);
  for (let e = 0; e < P.m; e++) {
    const v = r.voie[e];
    if (v >= 0 && (dominante[v] < 0 || expo(P.cls[e]) > expo(dominante[v]))) dominante[v] = P.cls[e];
  }
  if (COMPARER) {
    const r2 = calculer(Number(COMPARER)), haut = ids.slice(0, 20), haut2 = new Set(classees(r2).slice(0, 20));
    console.log(`stabilité : ${haut.filter(v => haut2.has(v)).length} des 20 premières voies communes aux pas ${PAS} et ${COMPARER} m`);
  }
  const voies = fusionner(r, ids).map(({ v, noeuds }) => {
    const coords = noeuds.map(n => [P.lon[n], P.lat[n]]), e = r.arete[v];
    return { g: C.encoderTroncons([coords]), score: r.score[v], l: Math.round(C.longueurTroncon(coords)),
      nom: P.noms[P.nom[e]] || "", cls: dominante[v], d: r.rep[v].map(x => Math.round(x * 1e5) / 1e5) };
  });
  const sortie = { genere: new Date().toISOString().slice(0, 10), osm: index.genere, pasM: PAS, departs: r.departs, ignores: r.ignores, voies };
  console.log(`${voies.length} voies classées ; tête : ${voies.slice(0, 5).map(v => `${v.nom || "(sans nom)"} ${Math.round(v.score / (r.departs - r.ignores) * 100)} %`).join(", ")}`);
  if (SORTIE) { fs.writeFileSync(SORTIE, JSON.stringify(sortie)); console.log(`écrit : ${SORTIE} (${(fs.statSync(SORTIE).size / 1024).toFixed(0)} Ko)`); }
})().catch(e => { console.error(e.message); process.exit(1); });

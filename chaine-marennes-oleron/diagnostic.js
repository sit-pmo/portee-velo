// Diagnostic « territoire de la demi-heure à vélo » pour les habitants d'un territoire (cadre du 13/10/2026).
//
// Pour chaque cellule habitée (Filosofi) et chaque fonction (accessibilite.json : alimentation, boulangerie et école
// à 15 min ; médecin, pharmacie, poste, collège, supermarché et sport à 30 min), trois cas :
//   0 tranquille     : on y va par les pistes ou des voies calmes dans le seuil (mesure « tranquille » de la carte) ;
//   1 route seulement : dans le seuil, mais en passant par une route exposée → investissement cyclable ;
//   2 trop loin      : au-delà du seuil même par la route → question de service, pas de piste.
// Maillons : pour chaque cas « route seulement », routes exposées du trajet le plus rapide vers le service le plus
// proche (hors 300 m autour du départ, comme la lecture confort). Une voie est classée par les habitants dont elle
// est sur un tel trajet, au moins une fonction (chaque habitant compté une fois par voie).
//
// Les morceaux de même classe qui se suivent sans autre route exposée au carrefour forment une seule voie (comme le
// classement des maillons manquants) : « aménager la D734 entre deux carrefours de départementales ».
//
//   node diagnostic.js <dossier du site> [oleron|pmo] [communes_cellules.json]
// Sortie : <site>/tuiles/diagnostic_<territoire>.json (lu par la page en vue territoire) et, à côté du script,
// diagnostic_<territoire>.geojson (une entité par cellule habitée, pour QGIS).
const fs = require("fs"), path = require("path");
const [site = "..", territoire = "oleron", fichierCommunes = path.join(__dirname, "communes_cellules.json")] = process.argv.slice(2);
const C = require(path.resolve(site, "coeur.js"));
const T = path.resolve(site, "tuiles");
const MAILLONS_MAX = 40;

(async () => {
  const index = JSON.parse(fs.readFileSync(path.join(T, "tuiles.json")));
  const terr = JSON.parse(fs.readFileSync(path.join(T, "territoires.json")))[territoire];
  if (!terr) throw new Error(`territoire inconnu : ${territoire}`);
  const Cm = C.preparerComptages(JSON.parse(fs.readFileSync(path.join(T, "accessibilite.json"))));
  const F = Cm.fonctions, nF = F.length;
  if (!nF) throw new Error("accessibilite.json sans fonctions : relancer comptages.py");
  const ipop = Cm.couches.findIndex(c => c.id === "population");
  if (ipop < 0) throw new Error("accessibilite.json sans couche population (Filosofi)");

  // Fenêtre fixe du territoire, comme la page en vue territoire
  const ch = new C.ChargeurTuiles({ index, lire: async (tx, ty) => fs.readFileSync(path.join(T, `${tx}_${ty}.bin`)) });
  const z = C.decoderMasque(terr.masque, index.grille);
  const P = (await ch.fenetre(z.centre[0], z.centre[1])).P, g = P.grille;
  C.appliquerMasque(g, z, index.grille);
  g.pro.confort = g.pro.tout; g.dist.confort = g.dist.tout;
  const Gs = { tout: C.construireGraphe(P, "tout"), amenage: C.construireGraphe(P, "amenage"), confort: C.construireGraphe(P, "confort") };
  const ix = C.indexerComptages(Cm, g), V = C.voies(P), groupe = grouperVoies(P, V);
  const communes = fs.existsSync(fichierCommunes) ? JSON.parse(fs.readFileSync(fichierCommunes)) : null;
  if (!communes) console.log(`(pas de ${fichierCommunes} : pas de diagnostic par commune)`);

  // Cellules habitées du territoire, avec leur commune et leur indice global (pour la page)
  const i0 = Math.round((g.w - index.grille.lon0) / g.dlon), j0 = Math.round((index.grille.lat0 - g.n) / g.dlat);
  const cel = new Map();
  const loc = ix.couches[ipop], v = Cm.couches[ipop].v, com = communes ? communes.c : null;
  for (let x = 0; x < loc.length; x++) {
    const c = loc[x];
    if (c < 0 || !g.ile[c]) continue;
    const e = cel.get(c) || { h: 0, commune: -1 };
    e.h += v[x];
    if (com && com[x] >= 0) e.commune = com[x];
    cel.set(c, e);
  }
  const cellules = [...cel.entries()].filter(([, e]) => e.h > 0).sort((a, b) => a[0] - b[0]);
  const total = cellules.reduce((s, [, e]) => s + e.h, 0);
  console.log(`${terr.nom} : ${cellules.length} cellules habitées, ${Math.round(total)} habitants, ${nF} fonctions`);

  // Agrégats
  const nC = communes ? communes.liste.length : 0;
  const glob = F.map(() => [0, 0, 0]), globToutes = [0, 0, 0];   // toutes : pire cas des fonctions
  const parCom = Array.from({ length: nC }, () => ({ h: 0, f: F.map(() => [0, 0, 0]), toutes: [0, 0, 0] }));
  const nV = V.liste.length, habVoie = new Float64Array(nV), habVoieF = F.map(() => new Float64Array(nV));
  const derniere = new Int32Array(nV).fill(-1), derniereF = F.map(() => new Int32Array(nV).fill(-1)), repH = new Float64Array(nV), rep = new Array(nV).fill(null);
  const libre = CFG_LIBRE();
  const entites = [], codesCas = [], t0 = Date.now();

  cellules.forEach(([c, e], n) => {
    const i = c % g.nx, j = (c - i) / g.nx, lon = g.w + (i + .5) * g.dlon, lat = g.n - (j + .5) * g.dlat;
    const M = C.minutesAcces(P, Gs, lon, lat, 3600);
    const K = C.casFonctions(Cm, ix, M, g.ile);
    const pire = Math.max(...K.map(k => k.cas));
    K.forEach((k, b) => {
      glob[b][k.cas] += e.h;
      if (e.commune >= 0) parCom[e.commune].f[b][k.cas] += e.h;
      if (k.cas !== 1 || !M.resTous) return;
      // Route seulement : routes exposées du trajet le plus rapide vers le service le plus proche
      const cible = g.pro.tout[k.celluleTous];
      if (cible < 0) return;
      for (const arc of C.itineraire(Gs.tout, M.resTous, cible)) {
        const ed = Gs.tout.arete[arc];
        if (!C.expose(P.cls[ed])) continue;
        if (libre(P, Gs.tout.source[arc], lon, lat) && libre(P, Gs.tout.cible[arc], lon, lat)) continue;
        const vo = V.voie[ed] >= 0 ? groupe[V.voie[ed]] : -1;
        if (vo < 0) continue;
        // Chaque habitant compte une fois par voie et par fonction (une voie a plusieurs arcs sur le trajet), et une
        // fois par voie toutes fonctions confondues
        if (derniereF[b][vo] !== c) { derniereF[b][vo] = c; habVoieF[b][vo] += e.h; }
        if (derniere[vo] !== c) { derniere[vo] = c; habVoie[vo] += e.h; }
        if (e.h > repH[vo]) { repH[vo] = e.h; rep[vo] = [+lon.toFixed(5), +lat.toFixed(5)]; }
      }
    });
    globToutes[pire] += e.h;
    if (e.commune >= 0) { parCom[e.commune].h += e.h; parCom[e.commune].toutes[pire] += e.h; }
    codesCas.push(K.map(k => k.cas).join(""));
    entites.push({ type: "Feature", geometry: { type: "Point", coordinates: [+lon.toFixed(5), +lat.toFixed(5)] },
      properties: { habitants: Math.round(e.h * 10) / 10, commune: e.commune >= 0 ? communes.liste[e.commune][1] : null,
        pire: C.CAS[pire], ...Object.fromEntries(F.flatMap((f, b) => [[`cas_${f.id}`, C.CAS[K[b].cas]],
          [`min_${f.id}`, isFinite(K[b].tous) ? Math.round(K[b].tous * 10) / 10 : null]])) } });
    if ((n + 1) % 1000 === 0) console.log(`  ${n + 1} / ${cellules.length} (${Math.round((Date.now() - t0) / 1000)} s)`);
  });

  // Maillons classés par habitants débloqués
  const ordre = [...Array(nV).keys()].filter(vo => habVoie[vo] > 0).sort((a, b) => habVoie[b] - habVoie[a]).slice(0, MAILLONS_MAX);
  const membres = new Map();
  for (let w = 0; w < V.liste.length; w++) if (groupe[w] >= 0) (membres.get(groupe[w]) || membres.set(groupe[w], []).get(groupe[w])).push(w);
  const maillons = ordre.map(vo => {
    const troncons = membres.get(vo).map(w => V.liste[w].map(n => [P.lon[n], P.lat[n]]));
    let l = 0;
    for (const t of troncons) for (let q = 1; q < t.length; q++) l += C.distanceM(...t[q - 1], ...t[q]);
    // Commune : celle de la cellule habitée la plus proche du milieu de la voie (repère pour la liste)
    const tm = troncons[Math.floor(troncons.length / 2)], mil = tm[Math.floor(tm.length / 2)];
    let best = Infinity, commune = null;
    for (const [c, e] of cellules) {
      if (e.commune < 0) continue;
      const i = c % g.nx, j = (c - i) / g.nx, d = C.distanceM(mil[0], mil[1], g.w + (i + .5) * g.dlon, g.n - (j + .5) * g.dlat);
      if (d < best) { best = d; commune = communes.liste[e.commune][1]; }
    }
    return { g: C.encoderTroncons(troncons), cls: P.cls[V.arete[vo]], l: Math.round(l), hab: Math.round(habVoie[vo]),
      f: F.map((_, b) => Math.round(habVoieF[b][vo])), d: rep[vo], commune };
  });

  const r = x => Math.round(x);
  const sortie = {
    version: 1, genere: new Date().toISOString().slice(0, 16), territoire: terr.nom, habitants: r(total),
    fonctions: F, cas: C.CAS, sources: Cm.sources,
    global: { f: glob.map(t => t.map(r)), toutes: globToutes.map(r) },
    communes: parCom.map((p, q) => ({ code: communes.liste[q][0], nom: communes.liste[q][1], h: r(p.h),
      f: p.f.map(t => t.map(r)), toutes: p.toutes.map(r) })).filter(p => p.h > 0).sort((a, b) => b.h - a.h),
    // Cellules : indice global (gj × W + gi) en différences, habitants × 10, un chiffre de cas par fonction
    cellules: {
      k: cellules.map(([c]) => { const i = c % g.nx, j = (c - i) / g.nx; return (j + j0) * Cm.W + (i + i0); })
        .map((k, q, a) => (q ? k - a[q - 1] : k)),
      h: cellules.map(([, e]) => Math.round(e.h * 10)), cas: codesCas,
    },
    maillons,
  };
  fs.writeFileSync(path.join(T, `diagnostic_${territoire}.json`), JSON.stringify(sortie));
  fs.writeFileSync(path.join(__dirname, `diagnostic_${territoire}.geojson`), JSON.stringify({ type: "FeatureCollection", features: entites }));

  const pct = x => (x / total * 100).toFixed(1).padStart(5);
  console.log(`\n${"Fonction".padEnd(22)} seuil  tranquille  route seul.  trop loin`);
  F.forEach((f, b) => console.log(`${f.libelle.padEnd(22)} ${String(f.seuil).padStart(3)}'    ${pct(glob[b][0])} %     ${pct(glob[b][1])} %    ${pct(glob[b][2])} %`));
  console.log(`${"Toutes".padEnd(22)}         ${pct(globToutes[0])} %     ${pct(globToutes[1])} %    ${pct(globToutes[2])} %`);
  console.log(`\nMaillons (habitants débloqués) : ${maillons.slice(0, 8).map(m => `${m.hab} (${(m.l / 1000).toFixed(1)} km)`).join(", ")}`);
  console.log(`→ tuiles/diagnostic_${territoire}.json (${(JSON.stringify(sortie).length / 1e6).toFixed(2)} Mo), ${Math.round((Date.now() - t0) / 1000)} s`);

  // Voies exposées regroupées : union des voies de même classe qui se touchent à un nœud où ne se rencontrent que
  // ces deux voies exposées. Rend groupe[voie] = représentant (-1 pour une voie non exposée).
  function grouperVoies(P, V) {
    const n = V.liste.length, parent = Int32Array.from({ length: n }, (_, i) => i);
    const racine = i => { while (parent[i] !== i) i = parent[i] = parent[parent[i]]; return i; };
    const cls = w => P.cls[V.arete[w]];
    const auNoeud = new Map();
    for (let w = 0; w < n; w++) {
      if (!C.expose(cls(w))) continue;
      const l = V.liste[w];
      for (const nd of [l[0], l[l.length - 1]]) (auNoeud.get(nd) || auNoeud.set(nd, []).get(nd)).push(w);
    }
    for (const ws of auNoeud.values()) {
      const u = [...new Set(ws)];
      if (u.length === 2 && cls(u[0]) === cls(u[1])) parent[racine(u[0])] = racine(u[1]);
    }
    return Int32Array.from({ length: n }, (_, w) => (C.expose(cls(w)) ? racine(w) : -1));
  }

  // Nœud à moins de departLibreM du départ (route exposée non pénalisée au départ, comme minutesTranquilles)
  function CFG_LIBRE() {
    const r0 = C.CFG.confort.departLibreM;
    return (P, n, lon, lat) => C.distanceM(lon, lat, P.lon[n], P.lat[n]) <= r0;
  }
})().catch(e => { console.error(e); process.exit(1); });

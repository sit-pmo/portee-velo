// Diagnostic « territoire de la demi-heure à vélo » par EPCI de Charente-Maritime (13/10/2026).
//
// Même calcul que diagnostic.js (vues île d'Oléron et 14 communes), étendu aux 13 EPCI du département pour la vue
// département de la page : l'onglet Services charge le bilan de l'EPCI où se trouve le départ D.
//
// Différences avec diagnostic.js :
// - Cellules habitées de l'EPCI : celles dont la commune (communes_cellules.json, centre de la cellule) est membre
//   de l'EPCI (epci17.json) ; pas de masque de territoire.
// - Fenêtre de calcul : pour chaque tuile qui contient des cellules habitées, la fenêtre 5 × 5 tuiles centrée sur
//   elle, comme la page pour un départ dans cette tuile (bloc « À portée ») : les cas sont ceux que la page affiche
//   depuis le même départ.
// - Équipements comptés partout (g.ile = terre de la fenêtre, comme la vue département), pas seulement dans l'EPCI :
//   on va au collège de l'EPCI voisin.
// - Maillons : les voies (et leur regroupement entre carrefours de routes exposées) sont celles d'une grande fenêtre
//   couvrant l'EPCI et deux tuiles autour, pour que la même route porte le même identifiant d'une fenêtre à l'autre ;
//   chaque arête d'une fenêtre est retrouvée dans la grande par les identifiants globaux de ses deux nœuds (deux
//   nœuds distincts peuvent partager des coordonnées).
//
//   node diagnostic_epci.js <dossier du site> [epci17.json] [communes_cellules.json] [--epci siren,siren]
// Sortie : <site>/tuiles/diagnostic_epci_<siren>.json (même format que diagnostic_<territoire>.json, lu par la page)
// et, à côté du script, diagnostic_epci_<siren>.geojson (une entité par cellule habitée, pour QGIS).
// Contours pour la page (EPCI du départ) : epci_contours.py → <site>/tuiles/epci.json.
const fs = require("fs"), path = require("path");
const args = process.argv.slice(2), opt = {};
for (let i = 0; i < args.length; i++) if (args[i].startsWith("--")) { opt[args[i].slice(2)] = args[i + 1]; args.splice(i, 2); i--; }
const [site = "..", fichierEpci = path.join(__dirname, "epci17.json"), fichierCommunes = path.join(__dirname, "communes_cellules.json")] = args;
const C = require(path.resolve(site, "coeur.js"));
const T = path.resolve(site, "tuiles");
const MAILLONS_MAX = 40, MARGE_TUILES = 2;

(async () => {
  const index = JSON.parse(fs.readFileSync(path.join(T, "tuiles.json")));
  const Cm = C.preparerComptages(JSON.parse(fs.readFileSync(path.join(T, "accessibilite.json"))));
  const F = Cm.fonctions, nF = F.length;
  if (!nF) throw new Error("accessibilite.json sans fonctions : relancer comptages.py");
  const ipop = Cm.couches.findIndex(c => c.id === "population");
  if (ipop < 0) throw new Error("accessibilite.json sans couche population (Filosofi)");
  const communes = JSON.parse(fs.readFileSync(fichierCommunes));
  const E = JSON.parse(fs.readFileSync(fichierEpci));
  const choix = opt.epci ? new Set(opt.epci.split(",")) : null;
  const G0 = index.grille, W = Cm.W, CT = G0.cellulesTuile;
  const lire = async (tx, ty) => fs.readFileSync(path.join(T, `${tx}_${ty}.bin`));
  const libre = CFG_LIBRE();

  for (const epci of E.epci) {
    if (choix && !choix.has(epci.code)) continue;
    const t0 = Date.now(), nom = epci.nom.replace(/"/g, "");

    // Cellules habitées de l'EPCI (indice global k = gj × W + gi), habitants et commune
    const membres = new Set(epci.communes);
    const garder = communes.liste.map(([code]) => membres.has(code));
    const cel = new Map();
    const kk = Cm.couches[ipop].k, v = Cm.couches[ipop].v, com = communes.c;
    for (let x = 0; x < kk.length; x++) {
      if (com[x] < 0 || !garder[com[x]] || !(v[x] > 0)) continue;
      const e = cel.get(kk[x]) || { h: 0, commune: com[x] };
      e.h += v[x];
      cel.set(kk[x], e);
    }
    const cellules = [...cel.entries()].sort((a, b) => a[0] - b[0]);
    const total = cellules.reduce((s, [, e]) => s + e.h, 0);
    const centre = k => { const gj = Math.floor(k / W), gi = k - gj * W; return [G0.lon0 + (gi + .5) * G0.dlon, G0.lat0 - (gj + .5) * G0.dlat, gi, gj]; };

    // Tuiles des cellules, et grande fenêtre (EPCI + MARGE_TUILES) pour des voies stables d'une fenêtre à l'autre
    const parTuile = new Map();
    let txa = Infinity, txb = -Infinity, tya = Infinity, tyb = -Infinity;
    for (const [k] of cellules) {
      const [, , gi, gj] = centre(k), tx = Math.floor(gi / CT), ty = Math.floor(gj / CT), cle = `${tx}_${ty}`;
      (parTuile.get(cle) || parTuile.set(cle, []).get(cle)).push(k);
      txa = Math.min(txa, tx); txb = Math.max(txb, tx); tya = Math.min(tya, ty); tyb = Math.max(tyb, ty);
    }
    const grand = new C.ChargeurTuiles({ index, lire, capacite: 1000 });
    const cles = [];
    for (let ty = tya - MARGE_TUILES; ty <= tyb + MARGE_TUILES; ty++)
      for (let tx = txa - MARGE_TUILES; tx <= txb + MARGE_TUILES; tx++) if (grand.existe.has(`${tx}_${ty}`)) cles.push(`${tx}_${ty}`);
    const Pb = (await grand.assembler(cles)).P;
    const Vb = C.voies(Pb), groupe = grouperVoies(Pb, Vb);
    const noeudB = new Map();
    for (let n = 0; n < Pb.n; n++) noeudB.set(Pb.global[n], n);   // identifiant global du nœud (stable d'une fenêtre à l'autre)
    const areteB = new Map();
    for (let e = 0; e < Pb.m; e++) {
      const a = Pb.a[e], b = Pb.b[e], cle = Math.min(a, b) * Pb.n + Math.max(a, b);
      if (!areteB.has(cle)) areteB.set(cle, e);
    }
    console.log(`${nom} : ${cellules.length} cellules habitées, ${Math.round(total)} habitants, ${parTuile.size} tuiles ; grande fenêtre ${cles.length} tuiles, ${Pb.n} nœuds`);

    // Agrégats
    const nC = communes.liste.length;
    const glob = F.map(() => [0, 0, 0]), globToutes = [0, 0, 0];   // toutes : pire cas des fonctions
    const parCom = Array.from({ length: nC }, () => ({ h: 0, f: F.map(() => [0, 0, 0]), toutes: [0, 0, 0] }));
    const nV = Vb.liste.length, habVoie = new Float64Array(nV), habVoieF = F.map(() => new Float64Array(nV));
    const derniere = new Float64Array(nV).fill(-1), derniereF = F.map(() => new Float64Array(nV).fill(-1));
    const repH = new Float64Array(nV), rep = new Array(nV).fill(null);
    const resultat = new Map();   // k → { cas (chaîne), K }
    let faites = 0, sansArete = 0;

    const fen = new C.ChargeurTuiles({ index, lire, capacite: 40 });
    for (const [cleT, ks] of [...parTuile.entries()].sort()) {
      const [tx, ty] = cleT.split("_").map(Number);
      const P = (await fen.fenetre(G0.lon0 + (tx + .5) * CT * G0.dlon, G0.lat0 - (ty + .5) * CT * G0.dlat)).P, g = P.grille;
      g.pro.confort = g.pro.tout; g.dist.confort = g.dist.tout;
      const Gs = { tout: C.construireGraphe(P, "tout"), amenage: C.construireGraphe(P, "amenage"), confort: C.construireGraphe(P, "confort") };
      const ix = C.indexerComptages(Cm, g);
      const i0 = Math.round((g.w - G0.lon0) / g.dlon), j0 = Math.round((G0.lat0 - g.n) / g.dlat);
      const versB = new Int32Array(P.n);
      for (let n = 0; n < P.n; n++) { const b = noeudB.get(P.global[n]); versB[n] = b === undefined ? -1 : b; }
      const voieDe = ed => {
        const a = versB[P.a[ed]], b = versB[P.b[ed]];
        if (a < 0 || b < 0) return -1;
        const eb = areteB.get(Math.min(a, b) * Pb.n + Math.max(a, b));
        if (eb === undefined) return -1;
        return Vb.voie[eb] >= 0 ? groupe[Vb.voie[eb]] : -1;
      };

      for (const k of ks) {
        const e = cel.get(k), [lon, lat] = centre(k);
        const M = C.minutesAcces(P, Gs, lon, lat, 3600);
        const K = C.casFonctions(Cm, ix, M, g.ile);
        const pire = Math.max(...K.map(q => q.cas));
        K.forEach((q, b) => {
          glob[b][q.cas] += e.h;
          parCom[e.commune].f[b][q.cas] += e.h;
          if (q.cas !== 1 || !M.resTous) return;
          // Route seulement : routes exposées du trajet le plus rapide vers le service le plus proche
          const cible = g.pro.tout[q.celluleTous];
          if (cible < 0) return;
          for (const arc of C.itineraire(Gs.tout, M.resTous, cible)) {
            const ed = Gs.tout.arete[arc];
            if (!C.expose(P.cls[ed])) continue;
            if (libre(P, Gs.tout.source[arc], lon, lat) && libre(P, Gs.tout.cible[arc], lon, lat)) continue;
            const vo = voieDe(ed);
            if (vo < 0) { sansArete++; continue; }
            // Chaque habitant compte une fois par voie et par fonction, et une fois par voie toutes fonctions confondues
            if (derniereF[b][vo] !== k) { derniereF[b][vo] = k; habVoieF[b][vo] += e.h; }
            if (derniere[vo] !== k) { derniere[vo] = k; habVoie[vo] += e.h; }
            if (e.h > repH[vo]) { repH[vo] = e.h; rep[vo] = [+lon.toFixed(5), +lat.toFixed(5)]; }
          }
        });
        globToutes[pire] += e.h;
        parCom[e.commune].h += e.h; parCom[e.commune].toutes[pire] += e.h;
        resultat.set(k, { cas: K.map(q => q.cas).join(""), K, pire });
        if (++faites % 2000 === 0) console.log(`  ${faites} / ${cellules.length} (${Math.round((Date.now() - t0) / 1000)} s)`);
      }
    }
    if (sansArete) console.log(`  (${sansArete} arcs exposés hors voie exposée, ignorés comme dans diagnostic.js)`);

    // Maillons classés par habitants débloqués
    const ordre = [...Array(nV).keys()].filter(vo => habVoie[vo] > 0).sort((a, b) => habVoie[b] - habVoie[a]).slice(0, MAILLONS_MAX);
    const membresV = new Map();
    for (let w = 0; w < nV; w++) if (groupe[w] >= 0) (membresV.get(groupe[w]) || membresV.set(groupe[w], []).get(groupe[w])).push(w);
    const maillons = ordre.map(vo => {
      const troncons = membresV.get(vo).map(w => Vb.liste[w].map(n => [Pb.lon[n], Pb.lat[n]]));
      let l = 0;
      for (const t of troncons) for (let q = 1; q < t.length; q++) l += C.distanceM(...t[q - 1], ...t[q]);
      // Commune : celle de la cellule habitée la plus proche du milieu de la voie (repère pour la liste)
      const tm = troncons[Math.floor(troncons.length / 2)], mil = tm[Math.floor(tm.length / 2)];
      let best = Infinity, commune = null;
      for (const [k, e] of cellules) {
        const [lo, la] = centre(k), d = C.distanceM(mil[0], mil[1], lo, la);
        if (d < best) { best = d; commune = communes.liste[e.commune][1]; }
      }
      return { g: C.encoderTroncons(troncons), cls: Pb.cls[Vb.arete[vo]], l: Math.round(l), hab: Math.round(habVoie[vo]),
        f: F.map((_, b) => Math.round(habVoieF[b][vo])), d: rep[vo], commune };
    });

    const r = x => Math.round(x);
    const sortie = {
      version: 1, genere: new Date().toISOString().slice(0, 16), territoire: nom, epci: epci.code, habitants: r(total),
      fonctions: F, cas: C.CAS, sources: [...Cm.sources, E.source],
      global: { f: glob.map(t => t.map(r)), toutes: globToutes.map(r) },
      communes: parCom.map((p, q) => ({ code: communes.liste[q][0], nom: communes.liste[q][1], h: r(p.h),
        f: p.f.map(t => t.map(r)), toutes: p.toutes.map(r) })).filter(p => p.h > 0).sort((a, b) => b.h - a.h),
      // Cellules : indice global (gj × W + gi) en différences, habitants × 10, un chiffre de cas par fonction
      cellules: {
        k: cellules.map(([k]) => k).map((k, q, a) => (q ? k - a[q - 1] : k)),
        h: cellules.map(([, e]) => Math.round(e.h * 10)), cas: cellules.map(([k]) => resultat.get(k).cas),
      },
      maillons,
    };
    const txt = JSON.stringify(sortie);
    fs.writeFileSync(path.join(T, `diagnostic_epci_${epci.code}.json`), txt);
    fs.writeFileSync(path.join(__dirname, `diagnostic_epci_${epci.code}.geojson`), JSON.stringify({ type: "FeatureCollection", features: cellules.map(([k, e]) => {
      const [lon, lat] = centre(k), { K, pire } = resultat.get(k);
      return { type: "Feature", geometry: { type: "Point", coordinates: [+lon.toFixed(5), +lat.toFixed(5)] },
        properties: { habitants: Math.round(e.h * 10) / 10, commune: communes.liste[e.commune][1], pire: C.CAS[pire],
          ...Object.fromEntries(F.flatMap((f, b) => [[`cas_${f.id}`, C.CAS[K[b].cas]], [`min_${f.id}`, isFinite(K[b].tous) ? Math.round(K[b].tous * 10) / 10 : null]])) } };
    }) }));

    const pct = x => (x / total * 100).toFixed(1).padStart(5);
    console.log(`${"Toutes les fonctions".padEnd(22)} ${pct(globToutes[0])} % tranquille, ${pct(globToutes[1])} % route seul., ${pct(globToutes[2])} % trop loin`);
    console.log(`→ tuiles/diagnostic_epci_${epci.code}.json (${(txt.length / 1e6).toFixed(2)} Mo), ${Math.round((Date.now() - t0) / 1000)} s\n`);
  }

  // Voies exposées regroupées (comme diagnostic.js) : union des voies de même classe qui se touchent à un nœud où ne
  // se rencontrent que ces deux voies exposées. Rend groupe[voie] = représentant (-1 pour une voie non exposée).
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

// Diagnostics « territoire de la demi-heure » par EPCI (phase 6, 17/10/2026), pour l'onglet Services de la page
// (index.html : epci.json, diagnostic_epci_<code>.json, installerDiag).
//
// Pour chaque habitant (cellule de 100 m de la couche population, Filosofi) et chaque fonction du quotidien
// (boulangerie, école, médecin… : acces/index.json, comptages_blocs.py), à son seuil (15 ou 30 min) :
//   0 tranquille     : l'équipement le plus proche est atteint par les pistes, ou par un trajet dont la version
//                      tranquille (route exposée comptée ×4) coûte au plus +20 % (ou +1 min), comme la teinte bleue
//                      de la carte (teinteConfort, coeur.js) ;
//   1 route seulement : atteint dans le seuil, mais en passant par une route exposée ;
//   2 trop loin       : même par la route, au-delà du seuil.
// Un calcul par habitant serait hors de portée (des millions de cellules) : on calcule à l'envers, depuis tous les
// équipements d'une fonction à la fois (Dijkstra multi-source sur le graphe transposé), le temps de chaque nœud
// jusqu'à l'équipement le plus proche. Différence avec la page (un départ) : on compare les plus proches de chaque
// sorte (le plus rapide, le plus tranquille) au lieu d'équipement à équipement, et sans la tolérance de 300 m autour
// du départ (CFG.confort.departLibreM) : le diagnostic est un peu plus sévère que le panneau d'un départ.
// Maillons : arêtes de route exposée des trajets les plus rapides des habitants « route seulement », chargées des
// habitants qui y passent, puis enchaînées en tronçons entre carrefours ; « toutes fonctions » = le maximum.
//
// Calcul par blocs de BLOC × BLOC tuiles (comme croisements.py), chacun avec une couronne d'une tuile : en 30 min à
// 20 km/h au plus on fait 10 km, une tuile. Deux étapes, la première parallélisable (workflow diagnostiquer.yml) :
//   node diagnostics.js calculer --tuiles ../tuiles/fr --diag cache/diag/fr --part 0/8
//   node diagnostics.js assembler --tuiles ../tuiles/fr --diag cache/diag/fr --sortie cache/diag/fr/sortie
// --diag : communes.json et cellules.bin de communes_epci.py ; parts écrites et relues dans ce même dossier.
"use strict";
const fs = require("fs");
const path = require("path");
const C = require("../../coeur.js");

const BLOC = 4;
const MODES = ["tout", "amenage", "confort"];
const MAILLONS_MAX = 40;                 // maillons gardés par fonction (et pour « toutes ») dans chaque EPCI
const CHARGE_MIN = 20;                    // habitants : en dessous, une arête n'est pas gardée (fichiers intermédiaires légers)

function args() {
  const a = { cmd: process.argv[2] };
  for (let i = 3; i < process.argv.length; i += 2) a[process.argv[i].replace(/^--/, "")] = process.argv[i + 1];
  return a;
}
const lireJSON = f => JSON.parse(fs.readFileSync(f, "utf8"));

// Graphe transposé : arc u → v de poids w devient v → u. Un Dijkstra depuis les équipements y donne, pour chaque
// nœud, le temps du trajet nœud → équipement le plus proche (sens uniques respectés).
function transposer(G) {
  const n = G.n, nA = G.cible.length, off = new Uint32Array(n + 1);
  for (let k = 0; k < nA; k++) off[G.cible[k] + 1]++;
  for (let i = 0; i < n; i++) off[i + 1] += off[i];
  const cur = off.slice(0, n), cible = new Int32Array(nA), source = new Int32Array(nA), poids = new Float32Array(nA), arete = new Int32Array(nA);
  for (let k = 0; k < nA; k++) {
    const u = G.source[k], v = G.cible[k], j = cur[v]++;
    cible[j] = u; source[j] = v; poids[j] = G.poids[k]; arete[j] = G.arete[k];
  }
  return { mode: G.mode, n, nArcs: nA, off, cible, source, poids, arete, actif: G.actif, accrochable: G.accrochable };
}

// Cellules habitées et leur commune (communes_epci.py) : recherche par dichotomie sur k
function lireCellules(f) {
  const buf = fs.readFileSync(f), n = buf.length / 12, k = new Float64Array(n), c = new Int32Array(n);
  for (let i = 0; i < n; i++) { k[i] = buf.readDoubleLE(i * 12); c[i] = buf.readInt32LE(i * 12 + 8); }
  return { k, c, commune(x) {
    let lo = 0, hi = n - 1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (k[m] < x) lo = m + 1; else if (k[m] > x) hi = m - 1; else return c[m]; }
    return -1;
  } };
}

// Tuiles décodées, gardées tant que les blocs suivants (parcourus ligne par ligne) peuvent en avoir besoin
async function lecteurTuiles(dossier, index) {
  const existe = new Set(index.tuiles.filter(t => t[3]).map(t => `${t[0]}_${t[1]}`)), cache = new Map();
  return {
    existe,
    async lire(tx, ty) {
      const cle = `${tx}_${ty}`;
      if (!existe.has(cle)) return null;
      if (!cache.has(cle)) cache.set(cle, C.decoderTuile(await C.decompresserTuile(fs.readFileSync(path.join(dossier, `${cle}.bin`)))));
      return cache.get(cle);
    },
    oublierAvant(ty) { for (const cle of cache.keys()) if (Number(cle.split("_")[1]) < ty) cache.delete(cle); },
  };
}

// Blocs acces/ (comptages_blocs.py) : équipements (k, f) et population (k, v), indices globaux absolus
function lecteurAcces(dossier) {
  const I = lireJSON(path.join(dossier, "acces", "index.json")), cache = new Map();
  const existe = new Set(I.blocs.map(([x, y]) => `${x}_${y}`));
  const cumul = d => { const t = new Float64Array(d.length); let s = 0; for (let i = 0; i < d.length; i++) { s += d[i]; t[i] = s; } return t; };
  return {
    I,
    bloc(bx, by) {
      const cle = `${bx}_${by}`;
      if (!existe.has(cle)) return null;
      if (!cache.has(cle)) {
        const D = lireJSON(path.join(dossier, "acces", `${cle}.json`)), pop = (D.couches || []).find(c => c.id === "population");
        const ech = (I.couches.find(c => c.id === "population") || {}).echelle || 1;
        cache.set(cle, { eq: D.equipements ? { k: cumul(D.equipements.k), f: D.equipements.f } : null,
                         pop: pop ? { k: cumul(pop.k), v: Float64Array.from(pop.v, x => x / ech) } : null });
      }
      return cache.get(cle);
    },
    oublierAvant(by) { for (const cle of cache.keys()) if (Number(cle.split("_")[1]) < by) cache.delete(cle); },
  };
}

// ---------------------------------------------------------------- calculer
async function calculer(a) {
  const dossier = a.tuiles, index = lireJSON(path.join(dossier, "tuiles.json"));
  C.definirGrille(index.grille);
  const g0 = index.grille, W = Math.round(360 / g0.dlon), T = g0.cellulesTuile;
  const tuiles = await lecteurTuiles(dossier, index), acces = lecteurAcces(dossier);
  const F = acces.I.fonctions, nF = F.length, seuilMax = Math.max(...F.map(f => f.seuil));
  const cellules = lireCellules(path.join(a.diag, "cellules.bin"));
  const [ip, np] = (a.part || "0/1").split("/").map(Number);
  // Blocs de tuiles qui ont de la population (blocs acces/ de même taille que les nôtres)
  if (acces.I.bloc !== BLOC) throw new Error(`blocs acces/ de ${acces.I.bloc} tuiles, attendu ${BLOC}`);
  const blocs = acces.I.blocs.map(([x, y]) => [x, y]).sort((p, q) => p[1] - q[1] || p[0] - q[0]).filter((_, i) => i % np === ip);
  const sortie = { k: [], h: [], c: [], cas: [] }, aretes = new Map();
  const t0 = Date.now();
  for (const [ib, [bx, by]] of blocs.entries()) {
    tuiles.oublierAvant(by * BLOC - 1); acces.oublierAvant(by - 1);
    const A = acces.bloc(bx, by);
    if (!A || !A.pop) continue;
    // Fenêtre : le bloc et une couronne d'une tuile
    const ts = [];
    for (let ty = by * BLOC - 1; ty <= by * BLOC + BLOC; ty++)
      for (let tx = bx * BLOC - 1; tx <= bx * BLOC + BLOC; tx++) { const t = await tuiles.lire(tx, ty); if (t) ts.push(t); }
    if (!ts.length) continue;
    const P = C.recollerTuiles(ts), g = P.grille;
    g.pro.confort = g.pro.tout; g.dist.confort = g.dist.tout;
    const gi0 = Math.round((g.w - g0.lon0) / g0.dlon), gj0 = Math.round((g0.lat0 - g.n) / g0.dlat);
    const local = k => { const gj = Math.floor(k / W), gi = k - gj * W, i = gi - gi0, j = gj - gj0; return i >= 0 && j >= 0 && i < g.nx && j < g.ny ? j * g.nx + i : -1; };
    const Gt = {};
    for (const m of MODES) Gt[m] = transposer(C.construireGraphe(P, m));
    // Sources : équipements de la fenêtre (blocs acces voisins), par fonction et par mode
    const src = F.map(() => ({ tout: [], amenage: [], confort: [] }));
    for (let yb = by - 1; yb <= by + 1; yb++) for (let xb = bx - 1; xb <= bx + 1; xb++) {
      const B = acces.bloc(xb, yb);
      if (!B || !B.eq) continue;
      for (let x = 0; x < B.eq.k.length; x++) {
        const fb = B.eq.f[x];
        if (!fb) continue;
        const c = local(B.eq.k[x]);
        if (c < 0) continue;
        for (const m of MODES) {
          const v = g.pro[m][c], d = g.dist[m][c];
          if (v < 0 || !(d <= C.CFG.accesMaxM[m])) continue;
          for (let b = 0; b < nF; b++) if (fb & (1 << b)) src[b][m].push([v, C.accesSecondes(d)]);
        }
      }
    }
    // Habitants du bloc (les cellules de la couronne sont traitées par leur propre bloc)
    const hab = [];
    for (let x = 0; x < A.pop.k.length; x++) {
      const c = local(A.pop.k[x]);
      if (c >= 0 && A.pop.v[x] > 0) hab.push({ k: A.pop.k[x], c, h: A.pop.v[x], cas: new Uint8Array(nF).fill(2) });
    }
    for (let b = 0; b < nF; b++) {
      const s = F[b].seuil, lim = s * 60, M = {}, R = {};
      for (const m of MODES) {
        if (!src[b][m].length) { M[m] = null; continue; }
        R[m] = C.dijkstra(Gt[m], src[b][m], 0, m === "confort" ? lim * C.CFG.confort.surcout[0] + 60 : lim);
        M[m] = C.tempsGrille(P, Gt[m], R[m]);
      }
      const pro = g.pro.tout, chemin = R.tout && R.tout.pred;
      for (const p of hab) {
        const tous = M.tout ? M.tout[p.c] : Infinity, pistes = M.amenage ? M.amenage[p.c] : Infinity, conf = M.confort ? M.confort[p.c] : Infinity;
        p.cas[b] = pistes <= s || (tous <= s && C.teinteConfort(tous, conf) === 0) ? 0 : tous <= s ? 1 : 2;
        // Route seulement : charge des arêtes exposées du trajet le plus rapide (arbre de R.tout, vers l'équipement)
        if (p.cas[b] !== 1 || !chemin) continue;
        for (let v = pro[p.c], pas = 0; v >= 0 && chemin[v] >= 0 && pas < 100000; pas++) {
          const k = chemin[v], e = Gt.tout.arete[k];
          if (C.expose(P.cls[e])) {
            const ga = P.global[P.a[e]], gb = P.global[P.b[e]], cle = Math.min(ga, gb) * 1e8 + Math.max(ga, gb);
            let A = aretes.get(cle);
            if (!A) aretes.set(cle, A = { xy: [P.lon[P.a[e]], P.lat[P.a[e]], P.lon[P.b[e]], P.lat[P.b[e]]].map(v => Math.round(v * 1e5)), cls: P.cls[e], lm: Math.round(P.lm[e]), f: new Float32Array(nF) });
            A.f[b] += p.h;
          }
          v = Gt.tout.source[k];               // nœud suivant, vers l'équipement
        }
      }
    }
    for (const p of hab) { sortie.k.push(p.k); sortie.h.push(Math.round(p.h * 10) / 10); sortie.c.push(cellules.commune(p.k)); sortie.cas.push(p.cas.join("")); }
    if (ib % 10 === 0) console.log(`bloc ${ib + 1}/${blocs.length} (${bx}, ${by}) : ${P.n} nœuds, ${hab.length} cellules habitées ; ${sortie.k.length} au total, ${aretes.size} arêtes chargées (${Math.round((Date.now() - t0) / 1000)} s)`);
  }
  const fichier = path.join(a.diag, `part_${ip}.json`);
  // Arêtes en colonnes (cle, coordonnées ×1e5, classe, longueur, charges par fonction à plat) : les mêmes peuvent venir
  // de plusieurs parts (trajets venus de blocs voisins), additionnées à l'assemblage
  const ar = { cle: [], xy: [], cls: [], lm: [], f: [] };
  for (const [cle, x] of aretes) {
    if (Math.max(...x.f) < CHARGE_MIN) continue;
    ar.cle.push(cle); ar.xy.push(...x.xy); ar.cls.push(x.cls); ar.lm.push(x.lm); ar.f.push(...Array.from(x.f, v => Math.round(v)));
  }
  fs.writeFileSync(fichier, JSON.stringify({ fonctions: F, cellules: sortie, aretes: ar }));
  console.log(`→ ${fichier} : ${sortie.k.length} cellules, ${ar.cle.length} arêtes chargées, en ${Math.round((Date.now() - t0) / 1000)} s`);
}

// ---------------------------------------------------------------- assembler
// Maillons : arêtes chargées d'un EPCI enchaînées entre carrefours (nœud de degré ≠ 2 dans le sous-graphe des arêtes
// chargées) et changements de classe ; charge du maillon = la plus forte de ses arêtes.
function maillons(liste, nF) {
  const cle = (lo, la) => `${Math.round(lo * 1e6)},${Math.round(la * 1e6)}`, adj = new Map();
  liste.forEach((x, i) => [0, 1].forEach(s => { const q = cle(x.lon[s], x.lat[s]); (adj.get(q) || adj.set(q, []).get(q)).push(i); }));
  const vu = new Uint8Array(liste.length), out = [];
  const suivant = (q, i) => { const l = adj.get(q); return l.length === 2 ? l.find(j => j !== i && !vu[j] && liste[j].cls === liste[i].cls) : undefined; };
  for (let i0 = 0; i0 < liste.length; i0++) {
    if (vu[i0]) continue;
    vu[i0] = 1;
    const x0 = liste[i0];
    let pts = [[x0.lon[0], x0.lat[0]], [x0.lon[1], x0.lat[1]]];
    const membres = [i0];
    // Prolonge des deux côtés tant que le nœud est de degré 2 et la classe la même
    for (const sens of [1, 0]) {
      let i = i0, q = cle(x0.lon[sens], x0.lat[sens]), j;
      while ((j = suivant(q, i)) !== undefined) {
        vu[j] = 1; membres.push(j);
        const y = liste[j], s = cle(y.lon[0], y.lat[0]) === q ? 1 : 0, p = [y.lon[s], y.lat[s]];
        if (sens === 1) pts.push(p); else pts.unshift(p);
        i = j; q = cle(y.lon[s], y.lat[s]);
      }
    }
    const f = Array.from({ length: nF }, (_, b) => Math.max(...membres.map(m => liste[m].f[b])));
    out.push({ pts, cls: x0.cls, l: Math.round(membres.reduce((s, m) => s + liste[m].lm, 0)), f, hab: Math.max(...f) });
  }
  return out;
}

function assembler(a) {
  const dossier = a.tuiles, index = lireJSON(path.join(dossier, "tuiles.json")), g0 = index.grille, W = Math.round(360 / g0.dlon);
  const communes = lireJSON(path.join(a.diag, "communes.json"));
  const epcis = lireJSON(path.join(a.diag, "epci.json")).epci, noms = new Map(epcis.map(e => [e.code, e.nom]));
  const parts = fs.readdirSync(a.diag).filter(f => /^part_\d+\.json$/.test(f)).map(f => lireJSON(path.join(a.diag, f)));
  if (!parts.length) throw new Error(`aucune part dans ${a.diag}`);
  const F = parts[0].fonctions, nF = F.length;
  const genere = new Date().toISOString().slice(0, 16).replace("T", " ");
  // Cellules par EPCI (commune → EPCI)
  const parEpci = new Map();
  for (const P of parts) {
    const S = P.cellules;
    for (let i = 0; i < S.k.length; i++) {
      const c = S.c[i], e = c >= 0 ? communes[c][2] : "";
      if (!e) continue;
      (parEpci.get(e) || parEpci.set(e, []).get(e)).push({ k: S.k[i], h: S.h[i], c, cas: S.cas[i] });
    }
  }
  // Arêtes chargées : EPCI de la cellule habitée la plus proche de leur milieu (cellule du milieu, puis voisines)
  const epciDeCellule = new Map();
  for (const [e, L] of parEpci) for (const p of L) epciDeCellule.set(p.k, [e, p.c]);
  const ou = (lon, lat) => {
    const gi = Math.floor((lon - g0.lon0) / g0.dlon), gj = Math.floor((g0.lat0 - lat) / g0.dlat);
    for (let r = 0; r <= 5; r++) for (let dj = -r; dj <= r; dj++) for (let di = -r; di <= r; di++) {
      if (Math.max(Math.abs(di), Math.abs(dj)) !== r) continue;
      const x = epciDeCellule.get((gj + dj) * W + gi + di);
      if (x) return x;
    }
    return null;
  };
  const toutes = new Map();
  for (const P of parts) {
    const A = P.aretes;
    for (let i = 0; i < A.cle.length; i++) {
      const f = A.f.slice(i * nF, (i + 1) * nF), x = toutes.get(A.cle[i]);
      if (x) { for (let b = 0; b < nF; b++) x.f[b] += f[b]; continue; }
      const xy = A.xy.slice(i * 4, i * 4 + 4).map(v => v / 1e5);
      toutes.set(A.cle[i], { lon: [xy[0], xy[2]], lat: [xy[1], xy[3]], cls: A.cls[i], lm: A.lm[i], f });
    }
  }
  const aretesEpci = new Map();
  for (const x of toutes.values()) {
    const o = ou((x.lon[0] + x.lon[1]) / 2, (x.lat[0] + x.lat[1]) / 2);
    if (o) (aretesEpci.get(o[0]) || aretesEpci.set(o[0], []).get(o[0])).push({ ...x, commune: o[1] });
  }
  toutes.clear();
  const sortie = a.sortie;
  fs.mkdirSync(sortie, { recursive: true });
  let n = 0;
  for (const [e, L] of parEpci) {
    L.sort((p, q) => p.k - q.k);
    const habitants = L.reduce((s, p) => s + p.h, 0);
    if (habitants < 1) continue;
    const pire = p => Math.max(...p.cas.split("").map(Number));
    const compter = (sel) => {
      const t = { toutes: [0, 0, 0], f: F.map(() => [0, 0, 0]) };
      for (const p of sel) { t.toutes[pire(p)] += p.h; for (let b = 0; b < nF; b++) t.f[b][Number(p.cas[b])] += p.h; }
      const r = v => Math.round(v);
      return { toutes: t.toutes.map(r), f: t.f.map(x => x.map(r)) };
    };
    const parCommune = new Map();
    for (const p of L) (parCommune.get(p.c) || parCommune.set(p.c, []).get(p.c)).push(p);
    const lesCommunes = [...parCommune].map(([c, sel]) => ({ code: communes[c][0], nom: communes[c][1], h: Math.round(sel.reduce((s, p) => s + p.h, 0)), ...compter(sel) }))
      .sort((x, y) => y.h - x.h);
    // Maillons : les plus chargés pour chaque fonction et pour « toutes », géométrie encodée (encoderTroncons)
    const ms = maillons(aretesEpci.get(e) || [], nF);
    const garde = new Set();
    for (const cle of [-1, ...F.keys()]) ms.map((m, i) => [cle < 0 ? m.hab : m.f[cle], i]).filter(([v]) => v > 0)
      .sort((x, y) => y[0] - x[0]).slice(0, MAILLONS_MAX).forEach(([, i]) => garde.add(i));
    const comDe = m => { const x = (aretesEpci.get(e) || []).find(y => y.lon[0] === m.pts[0][0] || y.lon[1] === m.pts[0][0]); return x ? communes[x.commune][1] : ""; };
    const lesMaillons = [...garde].map(i => ms[i]).map(m => ({ g: C.encoderTroncons([m.pts]), l: m.l, cls: m.cls, commune: comDe(m), hab: m.hab, f: m.f }));
    let prec = 0;
    const D = {
      version: 1, genere, epci: e, territoire: noms.get(e) || e, fonctions: F, habitants: Math.round(habitants),
      global: compter(L), communes: lesCommunes,
      cellules: { k: L.map(p => { const d = p.k - prec; prec = p.k; return d; }), h: L.map(p => Math.round(p.h * 10)), cas: L.map(p => p.cas) },
      maillons: lesMaillons,
    };
    fs.writeFileSync(path.join(sortie, `diagnostic_epci_${e}.json`), JSON.stringify(D));
    n++;
  }
  fs.copyFileSync(path.join(a.diag, "epci.json"), path.join(sortie, "epci.json"));
  console.log(`→ ${sortie} : ${n} diagnostics d'EPCI`);
}

const a = args();
if (a.cmd === "calculer") calculer(a).catch(e => { console.error(e); process.exit(1); });
else if (a.cmd === "assembler") assembler(a);
else { console.error("usage : node diagnostics.js calculer|assembler --tuiles … --diag … [--part i/N] [--sortie …]"); process.exit(2); }

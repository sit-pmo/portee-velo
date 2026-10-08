// coeur.js : cœur de calcul d'« À portée de vélo » (sans DOM, testable sous Node).
// Partagé par index.html et comparer.html : préparation des données, graphe, Dijkstra, temps par cellule,
// couverture et champ d'isolignes.
// ======================= CŒUR (sans DOM, testable sous Node) =======================
// Pénalités par classe : 0 piste, 1 bande, 2 route/chemin, 3 secondaire, 4 principale
const CFG = {
  penalites: {
    tout:    [1, 1.1, 1.5, 2, 3],      // principales sans aménagement : autorisées mais ×3 (sinon elles coupent le réseau)
    amenage: [1, 1, Infinity, Infinity, Infinity],
  },
  vitesseContreSensKmh: 5,             // sens unique pris à contre-sens : vélo poussé à pied, plutôt qu'interdit
  vitesseAccesKmh: 16,                 // accès au réseau hors voies (base vélo 20 km/h ×0,8)
  detourAcces: 1.3,
  accesMaxM: { tout: 2000, amenage: 800 },   // 2 km : couvre les marais de la Seudre, à 1,7 km de toute voie
};
const R_TERRE = 6371008.8, RAD = Math.PI / 180;

function distanceM(lon1, lat1, lon2, lat2) {
  const x = (lon2 - lon1) * RAD * Math.cos((lat1 + lat2) / 2 * RAD);
  const y = (lat2 - lat1) * RAD;
  return Math.hypot(x, y) * R_TERRE;
}
function accesSecondes(dM) { return dM * CFG.detourAcces / (CFG.vitesseAccesKmh / 3.6); }

// Décodage des tableaux compacts produits par build_graph_velo.py
function preparer(D) {
  const n = D.noeuds.lon.length, m = D.aretes.a.length;
  const lon = new Float64Array(n), lat = new Float64Array(n);
  for (let i = 0; i < n; i++) { lon[i] = D.noeuds.lon[i] / 1e5; lat[i] = D.noeuds.lat[i] / 1e5; }
  const lm = new Float32Array(m), cls = new Uint8Array(m), sens = new Uint8Array(m), vit = new Uint8Array(m);
  for (let e = 0; e < m; e++) {
    const t = D.aretes.attr[e];
    lm[e] = D.aretes.l[e] / 10; cls[e] = t & 7; sens[e] = (t >> 3) & 3; vit[e] = t >> 5;
  }
  const g = D.grille;
  return {
    n, m, lon, lat, lm, cls, sens, vit,
    a: Int32Array.from(D.aretes.a), b: Int32Array.from(D.aretes.b), nom: Int32Array.from(D.aretes.nom),
    noms: D.noms,
    grille: {
      w: g.w, n: g.n, dlon: g.dlon, dlat: g.dlat, nx: g.nx, ny: g.ny,
      dedans: Uint8Array.from(g.dedans),
      ile: Uint8Array.from(g.ile || g.dedans),   // couverture comptée sur le territoire seul (île + bassin de Marennes)
      pro: { tout: Int32Array.from(g.pro_tout), amenage: Int32Array.from(g.pro_amen) },
      dist: { tout: Float32Array.from(g.dist_tout), amenage: Float32Array.from(g.dist_amen) },
    },
  };
}

// Graphe orienté en CSR pour un mode ; poids = secondes (pénalité incluse).
// Chaque arête donne deux arcs : un sens unique se parcourt à contre-sens à pied (CFG.vitesseContreSensKmh).
function construireGraphe(P, mode) {
  const pen = CFG.penalites[mode], n = P.n, m = P.m;
  const off = new Uint32Array(n + 1);
  for (let e = 0; e < m; e++) {
    if (!isFinite(pen[P.cls[e]])) continue;
    off[P.a[e] + 1]++;
    off[P.b[e] + 1]++;
  }
  for (let i = 0; i < n; i++) off[i + 1] += off[i];
  const nArcs = off[n];
  const cible = new Int32Array(nArcs), source = new Int32Array(nArcs), arete = new Int32Array(nArcs);
  const poids = new Float32Array(nArcs), curseur = off.slice(0, n), actif = new Uint8Array(n);
  for (let e = 0; e < m; e++) {
    const p = pen[P.cls[e]];
    if (!isFinite(p)) continue;
    const s = P.lm[e] / (P.vit[e] / 3.6) * p, a = P.a[e], b = P.b[e];
    const pied = P.lm[e] / (CFG.vitesseContreSensKmh / 3.6);
    { const k = curseur[a]++; cible[k] = b; source[k] = a; poids[k] = P.sens[e] === 2 ? pied : s; arete[k] = e; }
    { const k = curseur[b]++; cible[k] = a; source[k] = b; poids[k] = P.sens[e] === 1 ? pied : s; arete[k] = e; }
    actif[a] = 1; actif[b] = 1;
  }
  return { mode, n, nArcs, off, cible, source, poids, arete, actif };
}

// Dijkstra avec tas binaire ; pred[v] = arc par lequel on arrive en v
function dijkstra(G, src, t0) {
  const dist = new Float64Array(G.n).fill(Infinity), pred = new Int32Array(G.n).fill(-1);
  let cap = 4096, hn = new Int32Array(cap), hd = new Float64Array(cap), taille = 0;
  const pousser = (v, d) => {
    if (taille === cap) {
      cap *= 2;
      const n2 = new Int32Array(cap); n2.set(hn); hn = n2;
      const d2 = new Float64Array(cap); d2.set(hd); hd = d2;
    }
    let i = taille++;
    while (i > 0) { const p = (i - 1) >> 1; if (hd[p] <= d) break; hn[i] = hn[p]; hd[i] = hd[p]; i = p; }
    hn[i] = v; hd[i] = d;
  };
  dist[src] = t0; pousser(src, t0);
  while (taille > 0) {
    const u = hn[0], d = hd[0];
    taille--;
    if (taille > 0) {
      const lv = hn[taille], ld = hd[taille];
      let i = 0;
      for (;;) {
        let c = 2 * i + 1;
        if (c >= taille) break;
        if (c + 1 < taille && hd[c + 1] < hd[c]) c++;
        if (hd[c] >= ld) break;
        hn[i] = hn[c]; hd[i] = hd[c]; i = c;
      }
      hn[i] = lv; hd[i] = ld;
    }
    if (d > dist[u]) continue;
    for (let k = G.off[u], fin = G.off[u + 1]; k < fin; k++) {
      const v = G.cible[k], nd = d + G.poids[k];
      if (nd < dist[v]) { dist[v] = nd; pred[v] = k; pousser(v, nd); }
    }
  }
  return { dist, pred, src };
}

// Nœud actif le plus proche d'un point (cellule de la grille + voisines, sinon recherche complète)
function accrocher(P, G, lon, lat) {
  const g = P.grille, pro = g.pro[G.mode];
  const i0 = Math.floor((lon - g.w) / g.dlon), j0 = Math.floor((g.n - lat) / g.dlat);
  let meilleur = -1, dMin = Infinity;
  for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) {
    const i = i0 + di, j = j0 + dj;
    if (i < 0 || j < 0 || i >= g.nx || j >= g.ny) continue;
    const v = pro[j * g.nx + i];
    if (v < 0 || !G.actif[v]) continue;
    const d = distanceM(lon, lat, P.lon[v], P.lat[v]);
    if (d < dMin) { dMin = d; meilleur = v; }
  }
  if (meilleur < 0) {
    for (let v = 0; v < P.n; v++) {
      if (!G.actif[v]) continue;
      const d = distanceM(lon, lat, P.lon[v], P.lat[v]);
      if (d < dMin) { dMin = d; meilleur = v; }
    }
  }
  return meilleur < 0 ? null : { noeud: meilleur, distM: dMin };
}

// Minutes par cellule : NaN hors zone de calcul, Infinity si hors d'atteinte
function tempsGrille(P, G, res) {
  const g = P.grille, N = g.nx * g.ny, out = new Float32Array(N);
  const pro = g.pro[G.mode], dd = g.dist[G.mode], max = CFG.accesMaxM[G.mode];
  for (let k = 0; k < N; k++) {
    if (!g.dedans[k]) { out[k] = NaN; continue; }
    const v = pro[k];
    if (v < 0 || dd[k] > max) { out[k] = Infinity; continue; }
    const t = res.dist[v];
    out[k] = isFinite(t) ? (t + accesSecondes(dd[k])) / 60 : Infinity;
  }
  return out;
}

// Arcs parcourus du départ jusqu'au nœud d'arrivée
function itineraire(G, res, arrivee) {
  const arcs = [];
  let v = arrivee;
  while (v !== res.src && res.pred[v] >= 0 && arcs.length < 1e6) {
    const k = res.pred[v];
    arcs.push(k);
    v = G.source[k];
  }
  return arcs.reverse();
}

// Regroupe les arcs en étapes (même classe et même nom), absorbe les bouts de moins de 40 m
function etapes(P, G, arcs) {
  const brut = [];
  for (const k of arcs) {
    const e = G.arete[k], der = brut[brut.length - 1];
    if (der && der.cls === P.cls[e] && der.nom === P.nom[e]) { der.m += P.lm[e]; der.s += G.poids[k]; }
    else brut.push({ cls: P.cls[e], nom: P.nom[e], m: P.lm[e], s: G.poids[k] });
  }
  const out = [];
  for (const et of brut) {
    const der = out[out.length - 1];
    if (der && (et.m < 40 || (der.cls === et.cls && der.nom === et.nom))) { der.m += et.m; der.s += et.s; }
    else out.push({ ...et });
  }
  return out;
}

// Part des cellules du territoire atteintes sous chaque seuil
function couverture(P, minutes, seuils) {
  const g = P.grille;
  let total = 0;
  const compte = seuils.map(() => 0);
  for (let k = 0; k < minutes.length; k++) {
    if (!g.ile[k]) continue;
    total++;
    const m = minutes[k];
    for (let s = 0; s < seuils.length; s++) if (m <= seuils[s]) compte[s]++;
  }
  return compte.map(c => (total ? c / total : 0));
}

// Champ continu pour les isolignes : les cellules hors réseau ou en mer reçoivent la valeur
// de la cellule atteinte la plus proche (propagation en largeur), puis on masque au tracé.
function champIsolignes(P, minutes) {
  const g = P.grille, N = g.nx * g.ny;
  const champ = new Float64Array(N).fill(NaN), masque = new Uint8Array(N), file = new Int32Array(N);
  let tete = 0, queue = 0;
  for (let k = 0; k < N; k++) {
    if (g.dedans[k] && isFinite(minutes[k])) { champ[k] = minutes[k]; masque[k] = 1; file[queue++] = k; }
  }
  if (!queue) return null;
  while (tete < queue) {
    const k = file[tete++], i = k % g.nx;
    const voisins = [i > 0 ? k - 1 : -1, i < g.nx - 1 ? k + 1 : -1, k - g.nx, k + g.nx];
    for (const v of voisins) {
      if (v < 0 || v >= N || !Number.isNaN(champ[v])) continue;
      champ[v] = champ[k]; file[queue++] = v;
    }
  }
  return { champ, masque };
}

if (typeof module !== "undefined") {
  module.exports = { CFG, preparer, construireGraphe, dijkstra, accrocher, tempsGrille, itineraire, etapes, couverture, champIsolignes, accesSecondes, distanceM };
}
// ===================================== FIN CŒUR =====================================

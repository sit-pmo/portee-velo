// coeur.js : cœur de calcul d'« À portée de vélo » (sans DOM, testable sous Node).
// Utilisé par index.html : préparation des données, graphe, Dijkstra, temps par cellule,
// couverture et champ d'isolignes.
// ======================= CŒUR (sans DOM, testable sous Node) =======================
// Pénalités par classe : 0 piste, 1 bande, 2 rues/service, 3 secondaire, 4 principale, 5 voie communale
// (unclassified), 6 chemin, 7 départementale tertiaire. Temps réels (11/10/2026) : +5 % sur route seulement, pour
// qu'à temps quasi égal le trajet prenne la piste qui longe la route (les majorations ×1,5 / ×2 / ×3 de la carte
// Marennes-Oléron d'origine donnaient des temps irréalistes ; test/non_regression.js les remet pour comparer).
// confort = trajet tranquille : chaque minute sur route exposée (CFG.confort.classes ≥ 2) compte pour 4.
const CFG = {
  penalites: {
    tout:    [1, 1, 1.05, 1.05, 1.05, 1.05, 1.05, 1.05],
    amenage: [1, 1, Infinity, Infinity, Infinity, Infinity, Infinity, Infinity],
    confort: [1, 1, 1.05, 4, 4, 1.05, 1.05, 4],
  },
  // Confort par classe (option A figée le 11/10/2026) : 0 aménagé, 1 calme, 2 exposé, 3 très exposé ; voies
  // communales calmes malgré les 80 km/h possibles en rural. Teinte : bleu si le trajet tranquille coûte au plus
  // +20 % (ou +1 min) de plus que le plus rapide, gris jusqu'à +50 %, orange au-delà. Autour du départ
  // (departLibreM), la route exposée n'est pas pénalisée : on part de chez soi par la rue qu'on a.
  confort: { classes: [0, 0, 1, 3, 3, 1, 1, 2], departLibreM: 300, surcout: [1.2, 1.5], surcoutMin: 1 },
  vitesseContreSensKmh: 5,             // sens unique pris à contre-sens : vélo poussé à pied, plutôt qu'interdit
  vitesseAccesKmh: 16,                 // accès au réseau hors voies (base vélo 20 km/h ×0,8)
  detourAcces: 1.3,
  accesMaxM: { tout: 2000, amenage: 800, confort: 2000 },   // 2 km : couvre les marais de la Seudre, à 1,7 km de toute voie
  traverseeMaxM: 30,                   // mode pistes : segment de route < 30 m au contact d'une piste = traversée praticable
  liaisonMaxM: 200,                    // mode pistes : liaison entre deux pistes par voie calme (rue, voie communale, chemin), longueur max
  // Coefficient de longueur par classe dans une liaison : voie calme 1, route exposée 2 (donc ≤ 100 m : 47 m de
  // départementale entre deux pistes au canal de Lachenaud coupaient le réseau). Pistes et bandes : sources, pas liaisons.
  // Tuiles du 17 : 2 rues/service, 3 secondaire, 4 principale, 5 unclassified, 6 chemins, 7 tertiaire
  liaisonFacteur: [0, 0, 1, 2, 2, 1, 1, 2],
  morceauMinM: 1000,                   // mode pistes : un départ ne s'accroche qu'à un morceau d'au moins 1 km de voies (longueur comptée dans la fenêtre chargée).
                                       // Avant le 11/10/2026 : 200 nœuds, ce qui écartait la piste de Sauzelle (3 km, 187 nœuds)
  // Simulation de tronçons (12/10/2026) : sommet raccordé au nœud existant le plus proche à 25 m au plus (comme
  // RACCORD_MAX_M de la carte d'origine) ; segments découpés tous les 100 m pour que les cellules longées aient un
  // nœud proche ; piste simulée classe 0, double sens, 20 km/h (vitesse de base)
  raccordMaxM: 25,
  pasTronconM: 100,
  vitesseSimuleeKmh: 20,
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
  const n = P.n, m = P.m;
  // Pénalité par arête. Mode pistes : un segment de route de moins de CFG.traverseeMaxM qui touche une piste
  // ou une bande est praticable (au coût du mode « tout ») : traversées de chaussée et raccords entre deux
  // pistes, qu'OSM classe en route. Sans ça, le réseau de pistes est en miettes (Lyon : 4 nœuds atteints
  // depuis la mairie). Un seul segment au contact d'une piste : la règle ne se propage pas le long d'une route.
  const penCls = CFG.penalites[mode], penE = new Float32Array(m);
  let touche = null;
  if (mode === "amenage") {
    touche = new Uint8Array(n);
    for (let e = 0; e < m; e++) if (isFinite(penCls[P.cls[e]])) { touche[P.a[e]] = 1; touche[P.b[e]] = 1; }
  }
  const liaison = touche ? liaisons(P, touche) : null;
  for (let e = 0; e < m; e++) {
    let p = penCls[P.cls[e]];
    if (!isFinite(p) && touche && ((P.lm[e] < CFG.traverseeMaxM && (touche[P.a[e]] || touche[P.b[e]])) || liaison[e]))
      p = CFG.penalites.tout[P.cls[e]];
    penE[e] = p;
  }
  const off = new Uint32Array(n + 1);
  for (let e = 0; e < m; e++) {
    if (!isFinite(penE[e])) continue;
    off[P.a[e] + 1]++;
    off[P.b[e] + 1]++;
  }
  for (let i = 0; i < n; i++) off[i + 1] += off[i];
  const nArcs = off[n];
  const cible = new Int32Array(nArcs), source = new Int32Array(nArcs), arete = new Int32Array(nArcs);
  const poids = new Float32Array(nArcs), curseur = off.slice(0, n), actif = new Uint8Array(n);
  for (let e = 0; e < m; e++) {
    const p = penE[e];
    if (!isFinite(p)) continue;
    const s = P.lm[e] / (P.vit[e] / 3.6) * p, a = P.a[e], b = P.b[e];
    const pied = P.lm[e] / (CFG.vitesseContreSensKmh / 3.6);
    { const k = curseur[a]++; cible[k] = b; source[k] = a; poids[k] = P.sens[e] === 2 ? pied : s; arete[k] = e; }
    { const k = curseur[b]++; cible[k] = a; source[k] = b; poids[k] = P.sens[e] === 1 ? pied : s; arete[k] = e; }
    actif[a] = 1; actif[b] = 1;
  }
  // Nœuds où un départ peut s'accrocher. Mode pistes : seulement les morceaux d'au moins
  // CFG.morceauMinM mètres ; sinon le départ tombe sur un bout de piste isolé à 50 m (Lyon : 7 nœuds)
  // alors que le vrai réseau (27 000 nœuds) est à 240 m. Mode tout : le graphe est déjà d'un seul tenant.
  let accrochable = actif;
  if (mode === "amenage") {
    const parent = new Int32Array(n);
    for (let v = 0; v < n; v++) parent[v] = v;
    const racine = x => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
    for (let k = 0; k < nArcs; k++) { const ra = racine(source[k]), rb = racine(cible[k]); if (ra !== rb) parent[ra] = rb; }
    const taille = new Float64Array(n);       // longueur (m) des arêtes praticables de chaque morceau
    for (let e = 0; e < m; e++) if (isFinite(penE[e])) taille[racine(P.a[e])] += P.lm[e];
    accrochable = new Uint8Array(n);
    for (let v = 0; v < n; v++) accrochable[v] = actif[v] && taille[racine(v)] >= CFG.morceauMinM ? 1 : 0;
  }
  return { mode, n, nArcs, off, cible, source, poids, arete, actif, accrochable };
}

// Mode pistes : arêtes de voirie formant un chemin court (longueur pondérée par CFG.liaisonFacteur ≤ CFG.liaisonMaxM)
// entre deux nœuds de piste distincts (touche[v] = 1). Dijkstra multi-source borné depuis toutes les pistes (source
// virtuelle n) ; une arête dont les deux bouts viennent de pistes différentes, avec d(u) + l + d(v) ≤ max, est une
// liaison : on la garde avec les arêtes qui remontent à chaque piste. Une rue en impasse au bout d'une piste n'est pas retenue.
function liaisons(P, touche) {
  const n = P.n, m = P.m, max = CFG.liaisonMaxM, f = CFG.liaisonFacteur;
  const calme = new Uint8Array(8);             // 1 = arête utilisable en liaison (toute voirie hors pistes et bandes)
  for (let c = 0; c < 8; c++) calme[c] = f[c] > 0 ? 1 : 0;
  const off = new Uint32Array(n + 2);
  let nSrc = 0;
  for (let v = 0; v < n; v++) if (touche[v]) nSrc++;
  for (let e = 0; e < m; e++) if (calme[P.cls[e]]) { off[P.a[e] + 1]++; off[P.b[e] + 1]++; }
  off[n + 1] = nSrc;
  for (let i = 0; i <= n; i++) off[i + 1] += off[i];
  const nArcs = off[n + 1], cible = new Int32Array(nArcs), source = new Int32Array(nArcs), poids = new Float32Array(nArcs), arete = new Int32Array(nArcs).fill(-1);
  const cur = off.slice(0, n + 1);
  for (let e = 0; e < m; e++) if (calme[P.cls[e]]) {
    const a = P.a[e], b = P.b[e];
    const l = P.lm[e] * f[P.cls[e]];
    let k = cur[a]++; cible[k] = b; source[k] = a; poids[k] = l; arete[k] = e;
    k = cur[b]++; cible[k] = a; source[k] = b; poids[k] = l; arete[k] = e;
  }
  for (let v = 0; v < n; v++) if (touche[v]) { const k = cur[n]++; cible[k] = v; source[k] = n; poids[k] = 0; }
  const res = dijkstra({ n: n + 1, off, cible, poids }, n, 0, max);
  // Piste d'origine de chaque nœud atteint, dans l'ordre des distances (le prédécesseur est déjà étiqueté)
  const atteints = [];
  for (let v = 0; v < n; v++) if (res.dist[v] <= max) atteints.push(v);
  atteints.sort((x, y) => res.dist[x] - res.dist[y]);
  const origine = new Int32Array(n).fill(-1);
  for (const v of atteints) { const k = res.pred[v]; origine[v] = source[k] === n ? v : origine[source[k]]; }
  const garder = new Uint8Array(m);
  const remonter = v => { while (res.pred[v] >= 0 && source[res.pred[v]] !== n) { const k = res.pred[v]; garder[arete[k]] = 1; v = source[k]; } };
  for (let e = 0; e < m; e++) {
    if (!calme[P.cls[e]]) continue;
    const a = P.a[e], b = P.b[e];
    if (origine[a] < 0 || origine[b] < 0 || origine[a] === origine[b]) continue;
    if (res.dist[a] + P.lm[e] * f[P.cls[e]] + res.dist[b] > max) continue;
    garder[e] = 1; remonter(a); remonter(b);
  }
  return garder;
}

// Dijkstra avec tas binaire ; pred[v] = arc par lequel on arrive en v.
// limite (secondes, facultative) : aucun nœud au-delà n'est atteint ; ceux en deçà ont leur temps exact.
// but (facultatif) : nœud d'arrivée ; le calcul s'arrête dès que son temps est définitif (itinéraire au-delà
// de la fenêtre de survol). ponytail: pas d'A* ; mesuré sur un couloir de 30 tuiles, l'heuristique coûtait plus
// qu'elle ne faisait gagner (57 ms contre 38 ms). À reconsidérer pour des couloirs bien plus grands.
function dijkstra(G, src, t0, limite = Infinity, but = -1) {
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
  // src : un nœud (départ au temps t0), ou une liste [[nœud, temps de départ], …] (départ d'avant et départ sur une
  // piste simulée, voir departs) ; res.src = premier nœud
  const sources = Array.isArray(src) ? src : [[src, t0]];
  for (const [v, t] of sources) if (t < dist[v]) { dist[v] = t; pousser(v, t); }
  if (Array.isArray(src)) src = src.length ? src[0][0] : -1;
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
    if (u === but) break;                      // arrivée sortie du tas : son temps est définitif
    for (let k = G.off[u], fin = G.off[u + 1]; k < fin; k++) {
      const v = G.cible[k], nd = d + G.poids[k];
      if (nd < dist[v] && nd <= limite) { dist[v] = nd; pred[v] = k; pousser(v, nd); }
    }
  }
  return { dist, pred, src };
}

// Nœud accrochable (G.accrochable) le plus proche d'un point, cherché par anneaux de cellules autour du point (chaque cellule
// connaît son nœud le plus proche), sans dépasser CFG.accesMaxM du mode : au-delà, null (« aucune voie »).
// Coût borné (~1 800 cellules au pire) quelle que soit la taille du graphe ; l'ancienne version
// parcourait tous les nœuds dès que les 3 × 3 cellules voisines étaient vides (mer, grands champs).
function accrocher(P, G, lon, lat) {
  const g = P.grille, pro = g.pro[G.mode], max = CFG.accesMaxM[G.mode];
  const i0 = Math.floor((lon - g.w) / g.dlon), j0 = Math.floor((g.n - lat) / g.dlat);
  // Taille d'une cellule en mètres (la plus petite des deux dimensions) → nombre d'anneaux pour couvrir max
  const cote = Math.min(g.dlat * RAD * R_TERRE, g.dlon * RAD * R_TERRE * Math.cos(lat * RAD));
  const anneaux = Math.ceil(max / cote) + 1;
  let meilleur = -1, dMin = Infinity;
  const essayer = (i, j) => {
    if (i < 0 || j < 0 || i >= g.nx || j >= g.ny) return;
    const v = pro[j * g.nx + i];
    if (v < 0 || !G.accrochable[v]) return;
    const d = distanceM(lon, lat, P.lon[v], P.lat[v]);
    if (d < dMin) { dMin = d; meilleur = v; }
  };
  for (let r = 0; r <= anneaux; r++) {
    // Un nœud trouvé à dMin ne peut être battu que par une cellule à moins de dMin : on arrête
    // dès que l'anneau est plus loin que le meilleur candidat (plus une cellule de marge)
    if ((r - 1) * cote > dMin) break;
    if (r === 0) { essayer(i0, j0); continue; }
    for (let k = -r; k <= r; k++) {
      essayer(i0 + k, j0 - r); essayer(i0 + k, j0 + r);
      if (k > -r && k < r) { essayer(i0 - r, j0 + k); essayer(i0 + r, j0 + k); }
    }
  }
  return meilleur < 0 || dMin > max ? null : { noeud: meilleur, distM: dMin };
}

// Départs possibles depuis (lon, lat) : le nœud accroché sur la grille et, dans une fenêtre avec tronçons simulés
// (grille.secours), aussi celui de la grille d'avant. Sans ce second départ, une piste simulée plus proche du départ
// mais reliée au réseau loin de là rallongeait les trajets (relecture du 12/10/2026). Liste vide : aucune voie.
function departs(P, G, lon, lat) {
  const a = accrocher(P, G, lon, lat), out = a ? [a] : [], s = P.grille.secours;
  if (s) {
    const b = accrocher({ ...P, grille: { ...P.grille, pro: s.pro, dist: s.dist } }, G, lon, lat);
    if (b && (!a || b.noeud !== a.noeud)) out.push(b);
  }
  return out;
}
const sourcesDijkstra = d => d.map(a => [a.noeud, accesSecondes(a.distM)]);

// Arêtes d'une classe (pistes, ou bandes) regroupées en lignes continues : on suit chaque enchaînement de nœuds
// de degré 2. Une ligne par arête donnait des dizaines de milliers de tracés à Leaflet (150 à 290 ms par fenêtre).
function lignesReseau(P, c) {
  const n = P.n, deg = new Uint16Array(n), aretes = [];
  for (let e = 0; e < P.m; e++) if (P.cls[e] === c) { aretes.push(e); deg[P.a[e]]++; deg[P.b[e]]++; }
  const off = new Uint32Array(n + 1);
  for (const e of aretes) { off[P.a[e] + 1]++; off[P.b[e] + 1]++; }
  for (let v = 0; v < n; v++) off[v + 1] += off[v];
  const adj = new Int32Array(off[n]), cur = off.slice(0, n);
  for (const e of aretes) { adj[cur[P.a[e]]++] = e; adj[cur[P.b[e]]++] = e; }
  const vu = new Uint8Array(P.m), lignes = [];
  const suivre = (v, e) => {
    const l = [[P.lat[v], P.lon[v]]];
    for (;;) {
      vu[e] = 1;
      const w = P.a[e] === v ? P.b[e] : P.a[e];
      l.push([P.lat[w], P.lon[w]]);
      if (deg[w] !== 2) break;                // extrémité ou carrefour : fin de la ligne
      let suite = -1;
      for (let k = off[w]; k < off[w + 1]; k++) if (!vu[adj[k]]) { suite = adj[k]; break; }
      if (suite < 0) break;
      v = w; e = suite;
    }
    lignes.push(l);
  };
  for (const e of aretes) for (const v of [P.a[e], P.b[e]]) if (!vu[e] && deg[v] !== 2) suivre(v, e);
  for (const e of aretes) if (!vu[e]) suivre(P.a[e], e);   // boucles fermées (aucun nœud de degré ≠ 2)
  return lignes;
}

// Confort : voie exposée (départementale, secondaire, principale) ?
function expose(c) { return CFG.confort.classes[c] >= 2; }

// Minutes « tranquilles » par cellule depuis (lon, lat) : temps pondéré du trajet qui évite la route exposée
// (graphe du mode « confort », même réseau que « tout »). Les arcs exposés à moins de departLibreM du départ (deux
// extrémités, à vol d'oiseau) reprennent le temps du mode « tout » le temps du calcul, puis leurs poids d'origine.
function minutesTranquilles(P, Gc, lon, lat, limiteS) {
  const dep = departs(P, Gc, lon, lat);
  if (!dep.length) return new Float32Array(P.grille.nx * P.grille.ny).fill(Infinity);
  const r = CFG.confort.departLibreM, dLat = r / 111000, dLon = dLat / Math.cos(lat * RAD);
  const proche = v => Math.abs(P.lat[v] - lat) < dLat && Math.abs(P.lon[v] - lon) < dLon && distanceM(lon, lat, P.lon[v], P.lat[v]) <= r;
  const libres = [], sauves = [];
  for (let v = 0; v < P.n; v++) {          // nœuds proches d'abord (bien moins nombreux que les arcs), puis leurs arcs
    if (!proche(v)) continue;
    for (let k = Gc.off[v]; k < Gc.off[v + 1]; k++) {
      const c = P.cls[Gc.arete[k]];
      if (expose(c) && proche(Gc.cible[k])) {
        libres.push(k); sauves.push(Gc.poids[k]);
        Gc.poids[k] *= CFG.penalites.tout[c] / CFG.penalites.confort[c];
      }
    }
  }
  const min = tempsGrille(P, Gc, dijkstra(Gc, sourcesDijkstra(dep), 0, limiteS));
  libres.forEach((k, i) => { Gc.poids[k] = sauves[i]; });   // valeurs d'origine (une division dériverait en Float32)
  return min;
}

// Teinte de confort d'une cellule : 0 bleu (tranquille), 1 gris, 2 orange, d'après le temps le plus rapide m et
// le temps tranquille mc (minutes)
function teinteConfort(m, mc) {
  const s = CFG.confort;
  return mc <= Math.max(m * s.surcout[0], m + s.surcoutMin) ? 0 : mc <= m * s.surcout[1] ? 1 : 2;
}

// Maillons manquants (traits noirs) depuis un départ : arcs de route exposée de l'arbre des plus rapides par lesquels
// passent les trajets vers au moins seuilKm2 de cellules orange (trajet tranquille > +50 %) à minutesMax au plus.
// Charge = cellules orange rattachées au nœud (pro.tout), remontée des feuilles vers la racine. Règle de la carte à
// un départ, partagée avec maillons.js (D4, 12/10/2026). Rend [{ arc, charge (km²) }], distances croissantes.
const CELLULE_KM2 = 0.0097;          // cellule de la grille France : 0,00125° × 0,0009° ≈ 97 m × 100 m
function traitsNoirs(P, G, res, min, minConf, minutesMax = 60, seuilKm2 = 10) {
  const pro = P.grille.pro.tout, charge = new Float32Array(P.n);
  for (let k = 0; k < min.length; k++) {
    const m = min[k];
    if (!(m <= minutesMax)) continue;
    if (teinteConfort(m, minConf[k]) === 2) charge[pro[k]]++;
  }
  const ordre = [];
  for (let v = 0; v < P.n; v++) if (isFinite(res.dist[v])) ordre.push(v);
  ordre.sort((a, b) => res.dist[a] - res.dist[b]);
  for (let i = ordre.length - 1; i >= 0; i--) { const v = ordre[i], k = res.pred[v]; if (k >= 0) charge[G.source[k]] += charge[v]; }
  const out = [];
  for (const v of ordre) {
    const k = res.pred[v];
    if (k < 0 || !expose(P.cls[G.arete[k]])) continue;
    const s = charge[v] * CELLULE_KM2;
    if (s >= seuilKm2) out.push({ arc: k, charge: s });
  }
  return out;
}

// Minutes par cellule : NaN hors zone de calcul, Infinity si hors d'atteinte.
// Fenêtre avec tronçons simulés (g.secours = grille d'avant) : la cellule garde le meilleur de son nœud d'avant et
// du nœud simulé qui l'a reprise ; un nœud simulé injoignable dans ce mode (piste reliée seulement par une route, en
// mode pistes) ne rallonge donc jamais un temps.
function tempsGrille(P, G, res) {
  const g = P.grille, N = g.nx * g.ny, out = new Float32Array(N);
  const pro = g.pro[G.mode], dd = g.dist[G.mode], max = CFG.accesMaxM[G.mode];
  const sp = g.secours && g.secours.pro[G.mode], sd = g.secours && g.secours.dist[G.mode];
  const temps = (v, d) => (v >= 0 && d <= max && isFinite(res.dist[v]) ? (res.dist[v] + accesSecondes(d)) / 60 : Infinity);
  for (let k = 0; k < N; k++) {
    if (!g.dedans[k]) { out[k] = NaN; continue; }
    out[k] = sp ? Math.min(temps(pro[k], dd[k]), temps(sp[k], sd[k])) : temps(pro[k], dd[k]);
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

// Aire (km²) d'une cellule de la ligne j : grille en degrés, l'aire dépend de la latitude de la ligne (cos φ)
function aireLigneKm2(g, j) {
  return (g.dlat * RAD * R_TERRE / 1000) * (g.dlon * RAD * R_TERRE / 1000) * Math.cos((g.n - (j + 0.5) * g.dlat) * RAD);
}

// Surface (km²) des cellules comptées (g.ile) atteintes sous chaque seuil ; c'est le score de la version France (km² terrestres).
function surfaceKm2(P, minutes, seuils) {
  const g = P.grille, surf = seuils.map(() => 0);
  for (let j = 0; j < g.ny; j++) {
    const aire = aireLigneKm2(g, j);
    for (let i = 0, k = j * g.nx; i < g.nx; i++, k++) {
      if (!g.ile[k]) continue;
      const m = minutes[k];
      for (let s = 0; s < seuils.length; s++) if (m <= seuils[s]) surf[s] += aire;
    }
  }
  return surf;
}

// Simulation : km² gagnés sous chaque seuil (après − avant)
function gainSurface(P, minAvant, minApres, seuils) {
  const a = surfaceKm2(P, minAvant, seuils), b = surfaceKm2(P, minApres, seuils);
  return b.map((x, i) => x - a[i]);
}

// Simulation, confort : km² passés au bleu (depuis gris, orange, ou non atteints en limiteMin) et passés d'orange à
// gris. avant / apres = { min, minConf } (temps le plus rapide et temps tranquille par cellule, en minutes).
function gainConfort(P, avant, apres, limiteMin = 60) {
  const g = P.grille, out = { bleu: 0, gris: 0 };
  const teinte = (r, k) => (r.min[k] <= limiteMin ? teinteConfort(r.min[k], r.minConf[k]) : -1);
  for (let j = 0; j < g.ny; j++) {
    const aire = aireLigneKm2(g, j);
    for (let i = 0, k = j * g.nx; i < g.nx; i++, k++) {
      if (!g.ile[k]) continue;
      const x = teinte(avant, k), y = teinte(apres, k);
      if (y === 0 && x !== 0) out.bleu += aire;
      else if (y === 1 && x === 2) out.gris += aire;
    }
  }
  return out;
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

// ======================= SCÉNARIO DE TRONÇONS SIMULÉS =======================
// Un tronçon = [[lon, lat], …] (WGS84, au moins 2 sommets) ; un scénario = liste de tronçons.

// Lambert 93 (EPSG:2154) → [lon, lat] WGS84 : projection conique conforme inverse, ellipsoïde GRS80, constantes
// IGN (notice ALG0004, latitude isométrique inverse par itération). RGF93 ≡ WGS84 au mètre : pas de changement de datum.
function l93VersWgs84(x, y) {
  const n = 0.7256077650, c = 11754255.426, xs = 700000, ys = 12655612.050, e = 0.0818191910428158;
  const dx = x - xs, dy = ys - y, R = Math.hypot(dx, dy);
  const lon = 3 + Math.atan(dx / dy) / n / RAD;
  const L = -Math.log(R / c) / n;                       // latitude isométrique
  let phi = 2 * Math.atan(Math.exp(L)) - Math.PI / 2;
  for (let i = 0; i < 20; i++) {
    const s = e * Math.sin(phi);
    const suivant = 2 * Math.atan(((1 + s) / (1 - s)) ** (e / 2) * Math.exp(L)) - Math.PI / 2;
    if (Math.abs(suivant - phi) < 1e-12) { phi = suivant; break; }
    phi = suivant;
  }
  return [lon, phi / RAD];
}

// Scénario ↔ texte du lien (t=) : polyline encodée de Google (précision 1e-5, lat puis lon, compatible avec les
// outils existants), un texte par tronçon, séparés par des virgules (hors de l'alphabet 63..126 de l'algorithme).
function encoderTroncons(troncons) {
  const enc = v => {
    v = v < 0 ? ~(v << 1) : v << 1;
    let s = "";
    while (v >= 0x20) { s += String.fromCharCode((0x20 | (v & 0x1f)) + 63); v >>= 5; }
    return s + String.fromCharCode(v + 63);
  };
  return troncons.map(t => {
    let pla = 0, plo = 0, s = "";
    for (const [lon, lat] of t) {
      const la = Math.round(lat * 1e5), lo = Math.round(lon * 1e5);
      s += enc(la - pla) + enc(lo - plo); pla = la; plo = lo;
    }
    return s;
  }).join(",");
}
// Inverse d'encoderTroncons ; lève une erreur sur un texte illisible, tronqué ou un tronçon de moins de 2 sommets
function decoderTroncons(s) {
  if (!s) return [];
  return s.split(",").map(p => {
    let i = 0, la = 0, lo = 0;
    const lire = () => {
      let v = 0, dec = 0, c;
      do {
        if (i >= p.length) throw new Error("tronçon tronqué");
        c = p.charCodeAt(i++) - 63;
        if (c < 0 || c > 63) throw new Error("caractère hors alphabet");
        v |= (c & 0x1f) << dec; dec += 5;
      } while (c >= 0x20);
      return v & 1 ? ~(v >> 1) : v >> 1;
    };
    const t = [];
    while (i < p.length) { la += lire(); lo += lire(); t.push([lo / 1e5, la / 1e5]); }
    if (t.length < 2) throw new Error("tronçon de moins de 2 sommets");
    return t;
  });
}

// GeoJSON (FeatureCollection, Feature ou géométrie) → tronçons WGS84. LineString et MultiLineString gardées (une
// partie = un tronçon), le reste compté dans `ignores`. Lambert 93 reconnu au champ crs (2154) ou, sans crs (export
// QGIS courant), à une coordonnée hors des bornes des degrés (|x| > 1000). Toute autre projection (crs CC46, 3857…)
// lève une erreur « projection non reconnue » : sans ce contrôle, les tronçons tombaient à des centaines de km.
function lireGeoJSON(o) {
  const lignes = [];
  let ignores = 0;
  const geometrie = g => {
    if (!g) ignores++;
    else if (g.type === "LineString") lignes.push(g.coordinates);
    else if (g.type === "MultiLineString") lignes.push(...g.coordinates);
    else if (g.type === "GeometryCollection") g.geometries.forEach(geometrie);
    else ignores++;
  };
  const parcourir = x => x.type === "FeatureCollection" ? x.features.forEach(parcourir) : x.type === "Feature" ? geometrie(x.geometry) : geometrie(x);
  parcourir(o);
  const nonReconnue = () => new Error("projection non reconnue : WGS84 (EPSG:4326) ou Lambert 93 (EPSG:2154) attendus");
  const crs = String((o.crs && o.crs.properties && o.crs.properties.name) || "");
  if (crs && !/2154|4326|CRS84/.test(crs)) throw nonReconnue();
  const l93 = /2154/.test(crs) || lignes.some(l => l.some(c => Math.abs(c[0]) > 1000));
  const troncons = [];
  for (const l of lignes) {
    const t = l.map(c => (l93 ? l93VersWgs84(c[0], c[1]) : [c[0], c[1]])).filter(([x, y]) => isFinite(x) && isFinite(y));
    if (t.length >= 2) troncons.push(t); else ignores++;
  }
  // Emprise large de la France métropolitaine : au-delà, ce n'était pas du Lambert 93 (ou pas du WGS84)
  if (troncons.some(t => t.some(([x, y]) => x < -6 || x > 10 || y < 41 || y > 52))) throw nonReconnue();
  return { troncons, ignores };
}

function longueurTroncon(t) {
  let l = 0;
  for (let i = 1; i < t.length; i++) l += distanceM(t[i - 1][0], t[i - 1][1], t[i][0], t[i][1]);
  return l;
}

// Tronçons → FeatureCollection WGS84 (export, à reprendre dans QGIS)
function versGeoJSON(troncons) {
  return { type: "FeatureCollection", features: troncons.map((t, i) => ({
    type: "Feature", properties: { nom: `Tronçon simulé ${i + 1}`, longueur_m: Math.round(longueurTroncon(t)) },
    geometry: { type: "LineString", coordinates: t.map(([lon, lat]) => [lon, lat]) },
  })) };
}

// Pour chaque point, nœud de P le plus proche à maxM au plus (-1 sinon). Seaux de maxM de côté, remplis avec les
// seuls nœuds du rectangle englobant les points : un parcours de P.n, puis 3 × 3 seaux par point.
function noeudsProches(P, points, maxM) {
  const out = new Int32Array(points.length).fill(-1);
  if (!points.length) return out;
  let w = Infinity, e = -Infinity, s = Infinity, n = -Infinity;
  for (const [lon, lat] of points) { w = Math.min(w, lon); e = Math.max(e, lon); s = Math.min(s, lat); n = Math.max(n, lat); }
  const pasLat = maxM / (R_TERRE * RAD), pasLon = pasLat / Math.cos(Math.max(Math.abs(s), Math.abs(n)) * RAD);
  w -= pasLon; e += pasLon; s -= pasLat; n += pasLat;
  const seaux = new Map(), cle = (i, j) => `${i}_${j}`;
  for (let v = 0; v < P.n; v++) {
    const lo = P.lon[v], la = P.lat[v];
    if (lo < w || lo > e || la < s || la > n) continue;
    const c = cle(Math.floor(lo / pasLon), Math.floor(la / pasLat));
    if (!seaux.has(c)) seaux.set(c, []);
    seaux.get(c).push(v);
  }
  points.forEach(([lon, lat], p) => {
    const i0 = Math.floor(lon / pasLon), j0 = Math.floor(lat / pasLat);
    let dMin = maxM;
    for (let di = -1; di <= 1; di++) for (let dj = -1; dj <= 1; dj++) {
      for (const v of seaux.get(cle(i0 + di, j0 + dj)) || []) {
        const d = distanceM(lon, lat, P.lon[v], P.lat[v]);
        if (d <= dMin) { dMin = d; out[p] = v; }
      }
    }
  });
  return out;
}

// « Aménager une voie en un clic » (12/10/2026) : voie existante (bande ou route, classe ≥ 1) la plus proche de
// (lon, lat) à maxM au plus, suivie dans les deux sens jusqu'aux carrefours (nœud où ne passent pas exactement deux
// voies de classe ≥ 1 ; les pistes ne coupent pas). Rend les sommets [[lon, lat], ...], null si aucune voie.
// ponytail: un chemin de service ou une entrée de parking OSM compte comme carrefour (bout de voie court) : on reclique
// sur la suite ; filtrer par nom de voie si ça gêne.
// Arête de classe ≥ clsMin la plus proche de (lon, lat) à maxM au plus (-1 sinon). Parcours de toutes les arêtes.
function areteProche(P, lon, lat, maxM, clsMin = 0) {
  const kx = Math.cos(lat * RAD) * RAD * R_TERRE, ky = RAD * R_TERRE;   // degrés → mètres autour du point
  const dLon = maxM / kx, dLat = maxM / ky;
  let eMin = -1, dMin = maxM;
  for (let e = 0; e < P.m; e++) {
    if (P.cls[e] < clsMin) continue;
    const a = P.a[e], b = P.b[e];
    if (Math.max(P.lon[a], P.lon[b]) < lon - dLon || Math.min(P.lon[a], P.lon[b]) > lon + dLon ||
        Math.max(P.lat[a], P.lat[b]) < lat - dLat || Math.min(P.lat[a], P.lat[b]) > lat + dLat) continue;
    // Distance point-segment dans le plan local (mètres)
    const ax = (P.lon[a] - lon) * kx, ay = (P.lat[a] - lat) * ky, bx = (P.lon[b] - lon) * kx, by = (P.lat[b] - lat) * ky;
    const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
    const s = l2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / l2)) : 0;
    const d = Math.hypot(ax + s * dx, ay + s * dy);
    if (d <= dMin) { dMin = d; eMin = e; }
  }
  return eMin;
}

// Pour chaque point, arête de P la plus proche à maxM au plus (-1 sinon), en un seul parcours des arêtes : points
// rangés en seaux de maxM de côté, chaque arête ne teste que les points des seaux que couvre son emprise élargie.
function aretesProches(P, points, maxM) {
  const out = new Int32Array(points.length).fill(-1), dMin = new Float64Array(points.length).fill(maxM);
  if (!points.length) return out;
  const lat0 = points[0][1], kx = Math.cos(lat0 * RAD) * RAD * R_TERRE, ky = RAD * R_TERRE;
  const pasLon = maxM / kx, pasLat = maxM / ky, seaux = new Map(), cle = (i, j) => `${i}_${j}`;
  points.forEach(([lon, lat], p) => {
    const c = cle(Math.floor(lon / pasLon), Math.floor(lat / pasLat));
    if (!seaux.has(c)) seaux.set(c, []);
    seaux.get(c).push(p);
  });
  let w = Infinity, ea = -Infinity, so = Infinity, no = -Infinity;     // emprise des points, élargie de maxM
  for (const [lon, lat] of points) { w = Math.min(w, lon); ea = Math.max(ea, lon); so = Math.min(so, lat); no = Math.max(no, lat); }
  w -= pasLon; ea += pasLon; so -= pasLat; no += pasLat;
  for (let e = 0; e < P.m; e++) {
    const a = P.a[e], b = P.b[e];
    if (Math.max(P.lon[a], P.lon[b]) < w || Math.min(P.lon[a], P.lon[b]) > ea || Math.max(P.lat[a], P.lat[b]) < so || Math.min(P.lat[a], P.lat[b]) > no) continue;
    const i0 = Math.floor(Math.min(P.lon[a], P.lon[b]) / pasLon) - 1, i1 = Math.floor(Math.max(P.lon[a], P.lon[b]) / pasLon) + 1;
    const j0 = Math.floor(Math.min(P.lat[a], P.lat[b]) / pasLat) - 1, j1 = Math.floor(Math.max(P.lat[a], P.lat[b]) / pasLat) + 1;
    if ((i1 - i0 + 1) * (j1 - j0 + 1) > 400) continue;   // arête de plus de ~400 m de long : ses nœuds suffisent
    for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) {
      for (const p of seaux.get(cle(i, j)) || []) {
        const [lon, lat] = points[p];
        const ax = (P.lon[a] - lon) * kx, ay = (P.lat[a] - lat) * ky, bx = (P.lon[b] - lon) * kx, by = (P.lat[b] - lat) * ky;
        const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
        const s = l2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / l2)) : 0;
        const d = Math.hypot(ax + s * dx, ay + s * dy);
        if (d <= dMin[p]) { dMin[p] = d; out[p] = e; }
      }
    }
  }
  return out;
}

// Voies entre carrefours : arêtes de classe ≥ 1 enchaînées par les nœuds où passent exactement deux voies de
// classe ≥ 1 (les pistes ne coupent pas). Unité de « Aménager une voie » et du classement D4.
// Rend { voie (indice par arête, -1 pour une piste), liste (nœuds de chaque voie dans l'ordre), arete (une par voie) }.
function voies(P) {
  const off = new Uint32Array(P.n + 1);
  for (let e = 0; e < P.m; e++) if (P.cls[e] >= 1) { off[P.a[e] + 1]++; off[P.b[e] + 1]++; }
  for (let v = 0; v < P.n; v++) off[v + 1] += off[v];
  const adj = new Int32Array(off[P.n]), cur = off.slice(0, P.n);
  for (let e = 0; e < P.m; e++) if (P.cls[e] >= 1) { adj[cur[P.a[e]]++] = e; adj[cur[P.b[e]]++] = e; }
  const voie = new Int32Array(P.m).fill(-1), liste = [], arete = [];
  // Nœuds rencontrés depuis v en s'éloignant de l'arête e, jusqu'au carrefour inclus
  const prolonger = (v, e, id) => {
    const l = [];
    for (;;) {
      if (off[v + 1] - off[v] !== 2) break;
      const suite = adj[off[v]] === e ? adj[off[v] + 1] : adj[off[v]];
      if (voie[suite] >= 0) break;                       // boucle refermée
      voie[suite] = id;
      v = P.a[suite] === v ? P.b[suite] : P.a[suite]; e = suite;
      l.push(v);
    }
    return l;
  };
  for (let e = 0; e < P.m; e++) {
    if (P.cls[e] < 1 || voie[e] >= 0) continue;
    const id = liste.length;
    voie[e] = id;
    const avant = prolonger(P.a[e], e, id).reverse(), apres = prolonger(P.b[e], e, id);
    liste.push([...avant, P.a[e], P.b[e], ...apres]);
    arete.push(e);
  }
  return { voie, liste, arete: Int32Array.from(arete) };
}

// Voie (de voies(P)) qui contient l'arête de classe ≥ 1 la plus proche du clic
function voieAuClic(P, lon, lat, maxM) {
  const eMin = areteProche(P, lon, lat, maxM, 1);
  if (eMin < 0) return null;
  const { voie, liste } = voies(P);
  return liste[voie[eMin]].map(v => [P.lon[v], P.lat[v]]);
}

// Grille : les cellules à maxM au plus du nœud v (lon, lat) le prennent si elles en sont plus proches que de leur nœud
function rattacher(g, pro, dist, v, lon, lat, maxM) {
  const dLat = maxM / (R_TERRE * RAD), dLon = dLat / Math.cos(lat * RAD);
  const i0 = Math.max(0, Math.floor((lon - dLon - g.w) / g.dlon)), i1 = Math.min(g.nx - 1, Math.floor((lon + dLon - g.w) / g.dlon));
  const j0 = Math.max(0, Math.floor((g.n - lat - dLat) / g.dlat)), j1 = Math.min(g.ny - 1, Math.floor((g.n - lat + dLat) / g.dlat));
  for (let j = j0; j <= j1; j++) {
    const clat = g.n - (j + 0.5) * g.dlat;
    for (let i = i0; i <= i1; i++) {
      const k = j * g.nx + i, d = distanceM(g.w + (i + 0.5) * g.dlon, clat, lon, lat);
      if (d <= maxM && d < dist[k]) { pro[k] = v; dist[k] = d; }
    }
  }
}

// Croisements des tronçons avec les arêtes de P : coupes[iT][j] = [{lon, lat, arete}, ...] le long du segment
// (j-1 → j) du tronçon iT, triés depuis le sommet j-1. Croisement strict (une voie seulement touchée ou longée n'en
// donne pas). ponytail: un croisement en pont ou en tunnel compte aussi comme raccord ; le réseau OSM ne dit pas
// lequel, et le cas reste rare pour une piste simulée.
function croisements(P, troncons) {
  const coupes = troncons.map(() => []);
  const segs = [];
  troncons.forEach((t, iT) => { for (let j = 1; j < t.length; j++) segs.push([iT, j, t[j - 1], t[j]]); });
  if (!segs.length) return coupes;
  let w = Infinity, e = -Infinity, s = Infinity, n = -Infinity;
  for (const t of troncons) for (const [lon, lat] of t) { w = Math.min(w, lon); e = Math.max(e, lon); s = Math.min(s, lat); n = Math.max(n, lat); }
  const kx = Math.cos((s + n) / 2 * RAD);       // plan local : longitudes ramenées à l'échelle des latitudes
  const ext = (p, q, r) => (q[0] - p[0]) * kx * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]) * kx;
  for (let k = 0; k < P.m; k++) {
    const a = [P.lon[P.a[k]], P.lat[P.a[k]]], b = [P.lon[P.b[k]], P.lat[P.b[k]]];
    if (Math.max(a[0], b[0]) < w || Math.min(a[0], b[0]) > e || Math.max(a[1], b[1]) < s || Math.min(a[1], b[1]) > n) continue;
    for (const [iT, j, p, q] of segs) {
      const d1 = ext(p, q, a), d2 = ext(p, q, b);
      if (d1 * d2 >= 0) continue;
      const d3 = ext(a, b, p), d4 = ext(a, b, q);
      if (d3 * d4 >= 0) continue;
      const u = d3 / (d3 - d4);                 // position du croisement sur le segment p → q
      (coupes[iT][j] ||= []).push({ u, lon: p[0] + u * (q[0] - p[0]), lat: p[1] + u * (q[1] - p[1]), arete: k });
    }
  }
  for (const c of coupes) for (const l of c) if (l) l.sort((x, y) => x.u - y.u);
  return coupes;
}

// Fenêtre P′ = P + tronçons simulés (P n'est pas modifié : il sert à l'« avant »). Chaque sommet devient un nœud,
// raccordé par une arête classe 0 au nœud de P le plus proche s'il est à CFG.raccordMaxM au plus ; chaque voie croisée
// par le tracé l'est aussi (croisements, depuis le 12/10/2026 : sans eux, une piste tracée en deux clics passait sur
// 19 voies sans s'y raccorder) ; les segments sont
// découpés tous les CFG.pasTronconM mètres ; les cellules proches se rattachent aux nouveaux nœuds (une piste en site
// propre doit servir aux cellules qu'elle traverse). Extrémités sans nœud proche rendues dans nonRaccordes, indices
// des tronçons sans aucun raccord (injoignables, sans effet) dans isoles.
// ponytail: copie complète des tableaux de la fenêtre (quelques dizaines de ms pour 1 M de nœuds), refaite à chaque
// modification du scénario ; à remplacer par un graphe additionnel si le scénario devait changer pendant le survol.
function ajouterTroncons(P, troncons) {
  if (!troncons.length) return { P, nonRaccordes: [], isoles: [], horsZone: [] };
  const nLon = [], nLat = [], ea = [], eb = [], el = [], nonRaccordes = [];
  const nouveau = (lon, lat) => { nLon.push(lon); nLat.push(lat); return P.n + nLon.length - 1; };
  const lier = (u, v, l) => { ea.push(u); eb.push(v); el.push(l); };
  const proches = noeudsProches(P, troncons.flat(), CFG.raccordMaxM);
  // Sommets sans nœud proche mais posés sur une voie aux nœuds espacés (route droite) : raccordés à cette voie
  const sansNoeud = troncons.flat().filter((_, k) => proches[k] < 0), surVoie = aretesProches(P, sansNoeud, CFG.raccordMaxM);
  const voieDe = new Map();
  { let k = 0; troncons.flat().forEach((pt, i) => { if (proches[i] < 0) voieDe.set(i, surVoie[k++]); }); }
  // Nœuds des tronçons raccordés au réseau : seuls eux reprennent des cellules (un tronçon sans aucun raccord est
  // injoignable : ses cellules seraient perdues, gain négatif vu le 12/10/2026)
  // Hors de la fenêtre chargée (marge de raccordMaxM), on ne voit pas les voies : un sommet n'y est ni raccordé ni
  // « libre » ; un tronçon sans raccord dont un sommet est dehors va dans horsZone, pas dans isoles
  const g0 = P.grille, mLat = CFG.raccordMaxM / (R_TERRE * RAD);
  const dedansFenetre = (lon, lat) => {
    const mLon = mLat / Math.cos(lat * RAD);
    return lon >= g0.w + mLon && lon <= g0.w + g0.nx * g0.dlon - mLon && lat <= g0.n - mLat && lat >= g0.n - g0.ny * g0.dlat + mLat;
  };
  const relies = [], isoles = [], horsZone = [];
  const coupes = croisements(P, troncons);
  let iSommet = 0;
  troncons.forEach((t, iT) => {
    let prec = -1, relie = false, dehors = false;
    const premier = P.n + nLon.length;
    // Points du tracé dans l'ordre : sommets (raccordés au nœud proche) et croisements avec une voie (raccordés aux
    // deux bouts de l'arête croisée : on peut quitter la piste là où elle coupe une route)
    const pts = [];
    t.forEach(([lon, lat], j) => {
      if (j > 0) for (const c of coupes[iT][j] || []) pts.push({ lon: c.lon, lat: c.lat, arete: c.arete });
      pts.push({ lon, lat, q: proches[iSommet], voie: voieDe.get(iSommet), bout: j === 0 || j === t.length - 1 });
      iSommet++;
    });
    pts.forEach(({ lon, lat, q, voie, arete, bout }, j) => {
      const v = nouveau(lon, lat), dedans = dedansFenetre(lon, lat);
      if (arete === undefined && q < 0 && voie >= 0 && dedans) arete = voie;
      if (!dedans) dehors = true;
      if (arete !== undefined) {
        relie = true;
        for (const w of [P.a[arete], P.b[arete]]) lier(v, w, distanceM(lon, lat, P.lon[w], P.lat[w]));
      } else if (q >= 0) { relie = true; lier(v, q, distanceM(lon, lat, P.lon[q], P.lat[q])); }
      else if (bout && dedans) nonRaccordes.push([lon, lat]);
      if (prec >= 0) {
        const { lon: lo0, lat: la0 } = pts[j - 1], d = distanceM(lo0, la0, lon, lat), k = Math.max(1, Math.ceil(d / CFG.pasTronconM));
        let u = prec;
        for (let s = 1; s < k; s++) { const w = nouveau(lo0 + (lon - lo0) * s / k, la0 + (lat - la0) * s / k); lier(u, w, d / k); u = w; }
        lier(u, v, d / k);
      }
      prec = v;
    });
    if (relie) for (let v = premier; v < P.n + nLon.length; v++) relies.push(v);
    else (dehors ? horsZone : isoles).push(iT);
  });
  const dn = nLon.length, dm = ea.length, n = P.n + dn, m = P.m + dm;
  const etendre = (Type, src, extra) => { const t = new Type(src.length + extra.length); t.set(src); t.set(extra, src.length); return t; };
  const rempli = (k, v) => new Array(k).fill(v);
  const Q = {
    ...P, n, m,
    lon: etendre(Float64Array, P.lon, nLon), lat: etendre(Float64Array, P.lat, nLat),
    a: etendre(Int32Array, P.a, ea), b: etendre(Int32Array, P.b, eb), lm: etendre(Float32Array, P.lm, el),
    cls: etendre(Uint8Array, P.cls, rempli(dm, 0)), sens: etendre(Uint8Array, P.sens, rempli(dm, 0)),
    vit: etendre(Uint8Array, P.vit, rempli(dm, CFG.vitesseSimuleeKmh)),
    nom: etendre(Int32Array, P.nom, rempli(dm, P.noms.length)), noms: [...P.noms, "Piste (simulée)"],
  };
  if (P.global) Q.global = etendre(Float64Array, P.global, rempli(dn, -1));
  const g = P.grille, pro = {}, dist = {};
  for (const mode of ["tout", "amenage"]) {
    pro[mode] = g.pro[mode].slice(); dist[mode] = g.dist[mode].slice();
    for (const v of relies) rattacher(g, pro[mode], dist[mode], v, Q.lon[v], Q.lat[v], CFG.accesMaxM[mode]);
  }
  if (g.pro.confort === g.pro.tout) { pro.confort = pro.tout; dist.confort = dist.tout; }   // alias posé par la page
  Q.grille = { ...g, pro, dist, secours: { pro: g.pro, dist: g.dist } };   // grille d'avant : voir tempsGrille
  return { P: Q, nonRaccordes, isoles, horsZone };
}

// Masque d'un territoire (territoires.json, tuiler.py) : plages alternées 0 / 1 sur un rectangle de la grille
// France ; centre = point d'où la page charge la fenêtre fixe du territoire. total = dénominateur des %.
function decoderMasque({ i0, j0, nx, ny, total, plages }, gi) {
  const m = new Uint8Array(nx * ny);
  for (let k = 0, pos = 0; k < plages.length; k += 2) { pos += plages[k]; m.fill(1, pos, pos + plages[k + 1]); pos += plages[k + 1]; }
  return { i0, j0, nx, ny, m, total, centre: [gi.lon0 + (i0 + nx / 2) * gi.dlon, gi.lat0 - (j0 + ny / 2) * gi.dlat] };
}
// Fenêtre ramenée au territoire : cellules hors masque ni colorées ni comptées (dedans = ile = masque)
function appliquerMasque(g, z, gi) {
  const di = Math.round((g.w - gi.lon0) / gi.dlon) - z.i0, dj = Math.round((gi.lat0 - g.n) / gi.dlat) - z.j0;
  const m = new Uint8Array(g.nx * g.ny);
  for (let j = 0; j < g.ny; j++) for (let i = 0; i < g.nx; i++) {
    const zi = i + di, zj = j + dj;
    m[j * g.nx + i] = zi >= 0 && zj >= 0 && zi < z.nx && zj < z.ny && z.m[zj * z.nx + zi] ? 1 : 0;
  }
  g.dedans = g.ile = m;
}

// ======================= TUILES (format v1, encodeur : format_tuile.py) =======================
// Grille de France en degrés ; doit suivre LON0, LAT0, DLON, DLAT, CELLULES_TUILE de format_tuile.py
const GRILLE_FRANCE = { lon0: -5.5, lat0: 51.5, dlon: 0.00125, dlat: 0.0009, cellulesTuile: 100, pasDistM: 10 };

// Décompresse un fichier de tuile (gzip) avec l'API native du navigateur / de Node ≥ 18
async function decompresserTuile(buffer) {
  const flux = new Blob([buffer]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(flux).arrayBuffer());
}

// Décode les octets décompressés d'une tuile (voir l'en-tête de format_tuile.py pour la spécification)
function decoderTuile(o) {
  if (o[0] !== 0x50 || o[1] !== 0x56 || o[2] !== 0x54 || o[3] !== 0x31) throw new Error("tuile : en-tête PVT1 absent");
  let p = 4;
  // Varint LEB128 ; multiplication plutôt que décalage : les identifiants dépassent 2^31 une fois zigzagués
  const lire = () => {
    let v = 0, mult = 1, octet;
    do { octet = o[p++]; v += (octet & 0x7f) * mult; mult *= 128; } while (octet & 0x80);
    if (p > o.length) throw new Error("tuile tronquée");
    return v;
  };
  const dezz = u => (u % 2 ? -(u + 1) / 2 : u / 2);
  const deltas = (n, Type) => {
    const t = new Type(n);
    let prec = 0;
    for (let i = 0; i < n; i++) { prec += dezz(lire()); t[i] = prec; }
    return t;
  };
  const version = lire();
  if (version !== 1) throw new Error(`tuile : version ${version} inconnue`);
  const tx = dezz(lire()), ty = dezz(lire()), base = lire(), n = lire(), m = lire(), ncx = lire(), ncy = lire();
  const lon = deltas(n, Int32Array), lat = deltas(n, Int32Array);
  const a = new Int32Array(m), b = new Int32Array(m), l = new Uint16Array(m), attr = new Uint16Array(m);   // classe | sens << 3 | vitesse km/h << 5 : dépasse 255 dès 8 km/h
  let precA = 0;
  for (let e = 0; e < m; e++) {
    precA += lire(); a[e] = base + precA;                 // a rendu en identifiant global
    b[e] = base + precA + dezz(lire());
    l[e] = lire(); attr[e] = lire();
  }
  const nc = ncx * ncy, terre = new Uint8Array(nc);
  for (let k = 0; k < nc; k++) terre[k] = lire();
  const pro = () => { const t = deltas(nc, Int32Array); for (let k = 0; k < nc; k++) t[k] -= 1; return t; };
  const proTout = pro(), proAmen = pro();
  const dist = () => { const t = new Uint16Array(nc); for (let k = 0; k < nc; k++) t[k] = lire() * GRILLE_FRANCE.pasDistM; return t; };
  const distTout = dist(), distAmen = dist();
  if (p !== o.length) throw new Error(`tuile : ${o.length - p} octets en trop`);
  return { tx, ty, base, n, m, lon, lat, a, b, l, attr, ncx, ncy, terre, proTout, proAmen, distTout, distAmen };
}

// Recolle des tuiles décodées en une structure identique à celle de preparer() : construireGraphe(),
// dijkstra(), accrocher() et tempsGrille() s'en servent sans changement.
// Une arête appartient à la tuile de son nœud a ; si son nœud b est dans une tuile non chargée, elle est
// écartée (elle est au bord de la fenêtre, hors du rayon de calcul). Grille = rectangle des tuiles reçues,
// les tuiles absentes y sont des cellules vides (ni terre ni nœud).
function recollerTuiles(tuiles) {
  const C = GRILLE_FRANCE.cellulesTuile;
  // Table identifiant global → index fusionné : les identifiants sont contigus par tuile (base .. base+n-1)
  // Les tuiles sans nœud (mer) partagent leur base avec la suivante : on les exclut de la table,
  // sinon la recherche peut tomber sur elles selon l'ordre de chargement (bug attrapé par le test d'ordre)
  const blocs = tuiles.filter(t => t.n > 0).map(t => ({ t, base: t.base, n: t.n })).sort((x, y) => x.base - y.base);
  let n = 0;
  for (const bl of blocs) { bl.decalage = n; n += bl.n; }
  const bases = Float64Array.from(blocs, bl => bl.base);
  const indice = gid => {
    let lo = 0, hi = blocs.length - 1;
    while (lo <= hi) {                                    // recherche dichotomique du bloc contenant gid
      const mi = (lo + hi) >> 1;
      if (bases[mi] <= gid) lo = mi + 1; else hi = mi - 1;
    }
    const bl = blocs[hi];
    return bl && gid < bl.base + bl.n ? bl.decalage + (gid - bl.base) : -1;
  };
  const lon = new Float64Array(n), lat = new Float64Array(n), global = new Float64Array(n);  // global : index fusionné → identifiant
  let mTotal = 0;
  for (const { t, decalage } of blocs) {
    for (let i = 0; i < t.n; i++) {
      lon[decalage + i] = t.lon[i] / 1e5; lat[decalage + i] = t.lat[i] / 1e5; global[decalage + i] = t.base + i;
    }
    mTotal += t.m;
  }
  const a = new Int32Array(mTotal), b = new Int32Array(mTotal), lm = new Float32Array(mTotal);
  const cls = new Uint8Array(mTotal), sens = new Uint8Array(mTotal), vit = new Uint8Array(mTotal);
  let m = 0, ecartees = 0;
  for (const { t } of blocs) {
    for (let e = 0; e < t.m; e++) {
      const ia = indice(t.a[e]), ib = indice(t.b[e]);
      if (ia < 0 || ib < 0) { ecartees++; continue; }
      const at = t.attr[e];
      a[m] = ia; b[m] = ib; lm[m] = t.l[e] / 10; cls[m] = at & 7; sens[m] = (at >> 3) & 3; vit[m] = at >> 5;
      m++;
    }
  }
  // Grille du rectangle couvrant les tuiles
  const txMin = Math.min(...tuiles.map(t => t.tx)), txMax = Math.max(...tuiles.map(t => t.tx));
  const tyMin = Math.min(...tuiles.map(t => t.ty)), tyMax = Math.max(...tuiles.map(t => t.ty));
  const nx = (txMax - txMin + 1) * C, ny = (tyMax - tyMin + 1) * C, N = nx * ny;
  const terre = new Uint8Array(N);
  const pro = { tout: new Int32Array(N).fill(-1), amenage: new Int32Array(N).fill(-1) };
  const dist = { tout: new Float32Array(N).fill(Infinity), amenage: new Float32Array(N).fill(Infinity) };
  for (const t of tuiles) {
    const ox = (t.tx - txMin) * C, oy = (t.ty - tyMin) * C;
    for (let j = 0; j < C; j++) for (let i = 0; i < C; i++) {
      const s = j * C + i, k = (oy + j) * nx + ox + i;
      terre[k] = t.terre[s];
      if (t.proTout[s] >= 0) { pro.tout[k] = indice(t.proTout[s]); dist.tout[k] = t.distTout[s]; }
      if (t.proAmen[s] >= 0) { pro.amenage[k] = indice(t.proAmen[s]); dist.amenage[k] = t.distAmen[s]; }
    }
  }
  return {
    n, m, lon, lat, global, lm: lm.subarray(0, m), cls: cls.subarray(0, m), sens: sens.subarray(0, m), vit: vit.subarray(0, m),
    a: a.subarray(0, m), b: b.subarray(0, m), nom: new Int32Array(m), noms: [""], ecartees,
    grille: {
      w: GRILLE_FRANCE.lon0 + txMin * C * GRILLE_FRANCE.dlon, n: GRILLE_FRANCE.lat0 - tyMin * C * GRILLE_FRANCE.dlat,
      dlon: GRILLE_FRANCE.dlon, dlat: GRILLE_FRANCE.dlat, nx, ny,
      dedans: terre, ile: terre, pro, dist,
    },
    idGlobal: g => indice(g),
  };
}

// Chargeur de fenêtre : les (2·rayon+1)² tuiles autour d'un point, recollées, avec un cache borné.
// Décisions de la revue d'ingénierie : tuile absente de l'index = vide, sans requête ; échec = un nouvel
// essai puis tuile vide et fenêtre marquée « partielle » (réessayée au prochain changement de fenêtre) ;
// cache de `capacite` tuiles décodées, la moins récemment utilisée sort en premier (D4, D5).
// Sans DOM : `lire(tx, ty)` rend les octets (fetch dans la page, fs dans les tests).
class ChargeurTuiles {
  constructor({ index, lire, decoder = async o => decoderTuile(await decompresserTuile(o)),
                rayon = 2, capacite = 60, delaiMs = 15000 }) {
    this.existe = new Set(index.tuiles.map(([tx, ty]) => `${tx}_${ty}`));
    Object.assign(this, { lire, decoder, rayon, capacite, delaiMs });
    this.cache = new Map();          // clé → tuile décodée ; l'ordre d'insertion sert d'ordre d'usage (LRU)
    this.enCours = new Map();        // clé → promesse : deux demandes simultanées ne lisent qu'une fois
    this.derniere = null;            // dernière fenêtre recollée, réutilisée si les tuiles n'ont pas changé
    this.lectures = 0;               // compteur (tests, débogage)
  }

  tuileDe(lon, lat) {
    const C = GRILLE_FRANCE.cellulesTuile;
    return [Math.floor((lon - GRILLE_FRANCE.lon0) / (C * GRILLE_FRANCE.dlon)),
            Math.floor((GRILLE_FRANCE.lat0 - lat) / (C * GRILLE_FRANCE.dlat))];
  }

  // Tuiles de l'index dans le carré de rayon r autour de (tx0, ty0)
  voisines(tx0, ty0, r) {
    const cles = [];
    for (let ty = ty0 - r; ty <= ty0 + r; ty++)
      for (let tx = tx0 - r; tx <= tx0 + r; tx++)
        if (this.existe.has(`${tx}_${ty}`)) cles.push(`${tx}_${ty}`);
    return cles;
  }

  // Une tuile : cache, sinon lecture (2 essais au plus, chacun limité à delaiMs) ; null si échec
  async tuile(cle) {
    if (this.cache.has(cle)) {
      const t = this.cache.get(cle);
      this.cache.delete(cle); this.cache.set(cle, t);          // remise en tête de l'ordre d'usage
      return t;
    }
    if (!this.enCours.has(cle)) {
      const [tx, ty] = cle.split("_").map(Number);
      const essai = () => {
        this.lectures++;
        let minuteur;
        return Promise.race([
          Promise.resolve().then(() => this.lire(tx, ty)).then(o => this.decoder(o)),
          new Promise((_, rejet) => { minuteur = setTimeout(() => rejet(new Error("délai dépassé")), this.delaiMs); }),
        ]).finally(() => clearTimeout(minuteur));
      };
      const p = essai().catch(essai).then(t => { this.cache.set(cle, t); return t; }, () => null)
        .finally(() => this.enCours.delete(cle));
      this.enCours.set(cle, p);
    }
    return this.enCours.get(cle);
  }

  // Retire les tuiles les moins récemment utilisées au-delà de la capacité, sans toucher à la fenêtre courante
  elaguer(garder) {
    for (const cle of this.cache.keys()) {
      if (this.cache.size <= this.capacite) break;
      if (!garder.has(cle)) this.cache.delete(cle);
    }
  }

  // Fenêtre autour d'un point : { P (structure de recollerTuiles, null si aucune tuile), partielle, manquantes }
  async fenetre(lon, lat) {
    const [tx0, ty0] = this.tuileDe(lon, lat);
    return { ...(await this.assembler(this.voisines(tx0, ty0, this.rayon))), centre: [tx0, ty0] };
  }

  // Couloir de tuiles autour du segment (lon1, lat1) → (lon2, lat2) : celles dont le centre est à moins de
  // demiLargeurKm du segment (plus une demi-diagonale de tuile). Sert aux itinéraires au-delà de la fenêtre.
  async couloir(lon1, lat1, lon2, lat2, demiLargeurKm) {
    const C = GRILLE_FRANCE.cellulesTuile, kx = Math.cos((lat1 + lat2) / 2 * RAD) * 111.32, ky = 111.32;
    const tl = C * GRILLE_FRANCE.dlon, th = C * GRILLE_FRANCE.dlat, marge = demiLargeurKm + Math.hypot(tl * kx, th * ky) / 2;
    // Distance (km) d'un point au segment, dans un repère plan local
    const ax = lon1 * kx, ay = lat1 * ky, bx = lon2 * kx, by = lat2 * ky, l2 = (bx - ax) ** 2 + (by - ay) ** 2 || 1e-9;
    const dSeg = (x, y) => { const s = Math.max(0, Math.min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / l2)); return Math.hypot(x - ax - s * (bx - ax), y - ay - s * (by - ay)); };
    const [txa, tya] = this.tuileDe(lon1, lat1), [txb, tyb] = this.tuileDe(lon2, lat2);
    const r = Math.ceil(marge / Math.min(tl * kx, th * ky)), cles = [];
    for (let ty = Math.min(tya, tyb) - r; ty <= Math.max(tya, tyb) + r; ty++)
      for (let tx = Math.min(txa, txb) - r; tx <= Math.max(txa, txb) + r; tx++) {
        const lon = GRILLE_FRANCE.lon0 + (tx + .5) * tl, lat = GRILLE_FRANCE.lat0 - (ty + .5) * th;
        if (this.existe.has(`${tx}_${ty}`) && dSeg(lon * kx, lat * ky) <= marge) cles.push(`${tx}_${ty}`);
      }
    return this.assembler(cles);
  }

  // Charge et recolle une liste de tuiles (réutilise le dernier recollage si c'est le même ensemble)
  async assembler(cles) {
    const tuiles = await Promise.all(cles.map(c => this.tuile(c)));
    const manquantes = cles.filter((_, i) => tuiles[i] === null);
    const recues = tuiles.filter(t => t !== null);
    this.elaguer(new Set(cles));
    const signature = cles.filter((_, i) => tuiles[i] !== null).join(" ");
    if (!this.derniere || this.derniere.signature !== signature) {
      this.derniere = { signature, P: recues.length ? recollerTuiles(recues) : null };
    }
    return { P: this.derniere.P, partielle: manquantes.length > 0, manquantes, nb: cles.length };
  }

  // Précharge la couronne suivante (rayon + 1) sans bloquer ; les échecs seront retentés à l'usage
  precharger(lon, lat) {
    const [tx0, ty0] = this.tuileDe(lon, lat);
    const fenetre = new Set(this.voisines(tx0, ty0, this.rayon));
    for (const cle of this.voisines(tx0, ty0, this.rayon + 1)) if (!fenetre.has(cle)) this.tuile(cle);
  }
}

if (typeof module !== "undefined") {
  module.exports = { CFG, preparer, construireGraphe, lignesReseau, expose, minutesTranquilles, teinteConfort, CELLULE_KM2, traitsNoirs, dijkstra, accrocher, tempsGrille, itineraire, etapes, couverture, champIsolignes, accesSecondes, distanceM,
    surfaceKm2, GRILLE_FRANCE, l93VersWgs84, encoderTroncons, decoderTroncons, lireGeoJSON, versGeoJSON, longueurTroncon, noeudsProches, areteProche, aretesProches, croisements, voies, voieAuClic, decoderMasque, appliquerMasque, ajouterTroncons, gainSurface, gainConfort, departs, sourcesDijkstra, decompresserTuile, decoderTuile, recollerTuiles, ChargeurTuiles };
}
// ===================================== FIN CŒUR =====================================

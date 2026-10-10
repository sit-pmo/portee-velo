// scenario.js : simulation de nouveaux aménagements cyclables pour « À portée de vélo » (sans DOM, testable sous Node).
// Ajoute au graphe préparé par coeur.js (preparer) des tronçons tracés à la main ou importés, raccordés au réseau
// existant, puis compare les temps de trajet avant et après. Utilisé par simuler.html.
//
// Raccordements : un nœud est créé à chaque croisement entre un tronçon projeté et une voie existante (comme pour le
// réseau consolidé), et chaque extrémité est reliée à la voie la plus proche si elle est à moins de SCN.accrocheM
// (et, si elle diffère, à la piste ou bande la plus proche, pour que le mode « Pistes et bandes » en profite aussi).
// L'arête existante coupée est conservée : on ajoute deux demi-arêtes vers le nœud de jonction.

const TYPES_PROJET = {
  piste:     { cls: 0, vit: 20, libelle: "Piste en enrobé" },
  stabilise: { cls: 0, vit: 15, libelle: "Piste en stabilisé" },
  bande:     { cls: 1, vit: 20, libelle: "Bande cyclable" },
};
const SCN = {
  pasM: 25,          // densification des tronçons projetés : un nœud tous les 25 m (grille de 100 m)
  accrocheM: 30,     // tolérance de raccordement des extrémités
};

// ---------- Coordonnées planes locales (mètres), suffisantes à l'échelle du territoire ----------
function repere(P) {
  const g = P.grille, lat0 = g.n - g.ny * g.dlat / 2;
  return { lon0: g.w, lat0, kx: Math.cos(lat0 * Math.PI / 180) * Math.PI / 180 * 6371008.8, ky: Math.PI / 180 * 6371008.8 };
}

// ---------- Index spatial des arêtes du réseau existant (cases de la grille de calcul) ----------
function indexerAretes(P) {
  const g = P.grille, nx = g.nx, ny = g.ny, N = nx * ny;
  const caseI = lon => Math.min(nx - 1, Math.max(0, Math.floor((lon - g.w) / g.dlon)));
  const caseJ = lat => Math.min(ny - 1, Math.max(0, Math.floor((g.n - lat) / g.dlat)));
  const bornes = new Int32Array(P.m * 4);
  const compte = new Uint32Array(N + 1);
  for (let e = 0; e < P.m; e++) {
    const a = P.a[e], b = P.b[e];
    const i0 = caseI(Math.min(P.lon[a], P.lon[b])), i1 = caseI(Math.max(P.lon[a], P.lon[b]));
    const j0 = caseJ(Math.max(P.lat[a], P.lat[b])), j1 = caseJ(Math.min(P.lat[a], P.lat[b]));
    bornes.set([i0, i1, j0, j1], e * 4);
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) compte[j * nx + i + 1]++;
  }
  for (let k = 0; k < N; k++) compte[k + 1] += compte[k];
  const liste = new Int32Array(compte[N]), cur = compte.slice(0, N);
  for (let e = 0; e < P.m; e++) {
    const [i0, i1, j0, j1] = bornes.subarray(e * 4, e * 4 + 4);
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) liste[cur[j * nx + i]++] = e;
  }
  return { m: P.m, off: compte, liste, caseI, caseJ, vu: new Uint32Array(P.m), tampon: 0 };
}

// Arêtes existantes dont la case croise l'emprise [lon0,lon1]×[lat0,lat1] (sans doublon)
function aretesDans(idx, P, lon0, lat0, lon1, lat1, rappel) {
  const nx = P.grille.nx;
  const i0 = idx.caseI(Math.min(lon0, lon1)), i1 = idx.caseI(Math.max(lon0, lon1));
  const j0 = idx.caseJ(Math.max(lat0, lat1)), j1 = idx.caseJ(Math.min(lat0, lat1));
  const marque = ++idx.tampon;
  if (marque === 0xffffffff) { idx.vu.fill(0); idx.tampon = 1; }
  for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
    const k = j * nx + i;
    for (let p = idx.off[k], fin = idx.off[k + 1]; p < fin; p++) {
      const e = idx.liste[p];
      if (idx.vu[e] === idx.tampon) continue;
      idx.vu[e] = idx.tampon;
      rappel(e);
    }
  }
}

// ---------- Construction d'un graphe « projet » = réseau existant + tronçons ----------
// traces : [{ type: "piste"|"stabilise"|"bande", nom, coords: [[lon, lat], ...], accroches: [bool par sommet] }]
// Renvoie un objet de même forme que preparer(), plus base, nBase, mBase et km (longueur projetée).
function construireScenario(P, idx, traces) {
  const R = repere(P);
  const X = lon => (lon - R.lon0) * R.kx, Y = lat => (lat - R.lat0) * R.ky;
  const nl = [], nb = [];                                         // nouveaux nœuds
  const ea = [], eb = [], el = [], ec = [], es = [], ev = [], en = [];   // nouvelles arêtes
  const noms = [];
  const lonDe = v => (v < P.n ? P.lon[v] : nl[v - P.n]), latDe = v => (v < P.n ? P.lat[v] : nb[v - P.n]);
  const att = e => (e < P.m
    ? { a: P.a[e], b: P.b[e], l: P.lm[e], c: P.cls[e], s: P.sens[e], v: P.vit[e], n: P.nom[e] }
    : { a: ea[e - P.m], b: eb[e - P.m], l: el[e - P.m], c: ec[e - P.m], s: es[e - P.m], v: ev[e - P.m], n: en[e - P.m] });
  const noeud = (lon, lat) => { nl.push(lon); nb.push(lat); return P.n + nl.length - 1; };
  const arete = (a, b, l, c, s, v, n) => { ea.push(a); eb.push(b); el.push(Math.max(0.1, l)); ec.push(c); es.push(s); ev.push(v); en.push(n); return P.m + ea.length - 1; };

  // Jonction sur l'arête e au paramètre t (0 = a, 1 = b) : nœud existant aux bouts, sinon nouveau nœud + deux demi-arêtes
  const jonctions = new Map();
  function jonction(e, t) {
    const A = att(e);
    if (t * A.l < 1) return A.a;
    if ((1 - t) * A.l < 1) return A.b;
    const cle = e + ":" + Math.round(t * A.l);
    if (jonctions.has(cle)) return jonctions.get(cle);
    const J = noeud(lonDe(A.a) + t * (lonDe(A.b) - lonDe(A.a)), latDe(A.a) + t * (latDe(A.b) - latDe(A.a)));
    arete(A.a, J, A.l * t, A.c, A.s, A.v, A.n);
    arete(J, A.b, A.l * (1 - t), A.c, A.s, A.v, A.n);
    jonctions.set(cle, J);
    return J;
  }

  // Parcourt les arêtes existantes et celles des tronçons déjà ajoutés, dans une emprise
  function pourAretes(lon0, lat0, lon1, lat1, exclusDepuis, rappel) {
    aretesDans(idx, P, lon0, lat0, lon1, lat1, rappel);
    for (let e = P.m; e < exclusDepuis; e++) rappel(e);
  }

  // Point le plus proche sur une arête (plan local) : { e, t, d }
  function plusProche(lon, lat, maxM, filtre, exclusDepuis) {
    const dl = maxM / R.kx, dt = maxM / R.ky, px = X(lon), py = Y(lat);
    let mieux = null;
    pourAretes(lon - dl, lat - dt, lon + dl, lat + dt, exclusDepuis, e => {
      const A = att(e);
      if (filtre && !filtre(A.c)) return;
      const ax = X(lonDe(A.a)), ay = Y(latDe(A.a)), bx = X(lonDe(A.b)), by = Y(latDe(A.b));
      const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      const t = L2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) : 0;
      const d = Math.hypot(ax + t * dx - px, ay + t * dy - py);
      if (d <= maxM && (!mieux || d < mieux.d)) mieux = { e, t, d };
    });
    return mieux;
  }

  let km = 0;
  traces.forEach((tr, numero) => {
    const T = TYPES_PROJET[tr.type] || TYPES_PROJET.piste, c = tr.coords;
    if (!c || c.length < 2) return;
    const nomIdx = P.noms.length + noms.length;
    noms.push(tr.nom || `Projet ${numero + 1}`);
    const debutTrace = P.m + ea.length;          // arêtes de ce tronçon : exclues de ses propres croisements
    const relier = (u, v, l) => { if (u !== v) arete(u, v, l, T.cls, 0, T.vit, nomIdx); };

    // Sommet du tronçon : extrémités (et sommets accrochés à l'écran) raccordés à la voie la plus proche
    function sommet(k) {
      const [lon, lat] = c[k], bout = k === 0 || k === c.length - 1;
      if (!bout && !(tr.accroches && tr.accroches[k])) return noeud(lon, lat);
      const cibles = [plusProche(lon, lat, SCN.accrocheM, null, debutTrace)];
      const amen = plusProche(lon, lat, SCN.accrocheM, cls => cls <= 1, debutTrace);
      if (amen && (!cibles[0] || amen.e !== cibles[0].e)) cibles.push(amen);
      let v = -1;
      for (const p of cibles) {
        if (!p) continue;
        const J = jonction(p.e, p.t);
        if (v < 0 && p.d < 1) { v = J; continue; }    // sommet posé sur la voie : on réutilise le nœud
        if (v < 0) v = noeud(lon, lat);
        relier(v, J, p.d);
      }
      return v < 0 ? noeud(lon, lat) : v;
    }

    let prec = sommet(0);
    for (let k = 0; k < c.length - 1; k++) {
      const [lo0, la0] = c[k], [lo1, la1] = c[k + 1];
      const px = X(lo0), py = Y(la0), rx = X(lo1) - px, ry = Y(la1) - py, L = Math.hypot(rx, ry);
      km += L / 1000;
      // Points intermédiaires : densification + croisements avec les voies (y compris les tronçons précédents)
      const pts = [];
      const nDiv = Math.max(1, Math.ceil(L / SCN.pasM));
      for (let j = 1; j < nDiv; j++) pts.push({ u: j / nDiv });
      pourAretes(lo0, la0, lo1, la1, debutTrace, e => {
        const A = att(e);
        const qx = X(lonDe(A.a)), qy = Y(latDe(A.a)), sx = X(lonDe(A.b)) - qx, sy = Y(latDe(A.b)) - qy;
        const den = rx * sy - ry * sx;
        if (Math.abs(den) < 1e-9) return;                          // parallèles
        const u = ((qx - px) * sy - (qy - py) * sx) / den, t = ((qx - px) * ry - (qy - py) * rx) / den;
        if (u <= 1e-6 || u >= 1 - 1e-6 || t < -1e-6 || t > 1 + 1e-6) return;
        pts.push({ u, e, t: Math.max(0, Math.min(1, t)) });
      });
      pts.sort((p, q) => p.u - q.u);
      let uPrec = 0;
      for (const p of pts) {
        const v = p.e === undefined ? noeud(lo0 + p.u * (lo1 - lo0), la0 + p.u * (la1 - la0)) : jonction(p.e, p.t);
        if (v === prec) continue;
        relier(prec, v, (p.u - uPrec) * L);
        prec = v; uPrec = p.u;
      }
      const fin = sommet(k + 1);
      relier(prec, fin, (1 - uPrec) * L);
      prec = fin;
    }
  });

  // Tableaux du graphe projet
  const n = P.n + nl.length, m = P.m + ea.length;
  const etendre = (Type, base, ajout) => { const t = new Type(base.length + ajout.length); t.set(base); t.set(ajout, base.length); return t; };
  const S = {
    n, m,
    lon: etendre(Float64Array, P.lon, nl), lat: etendre(Float64Array, P.lat, nb),
    lm: etendre(Float32Array, P.lm, el), cls: etendre(Uint8Array, P.cls, ec), sens: etendre(Uint8Array, P.sens, es),
    vit: etendre(Uint8Array, P.vit, ev), a: etendre(Int32Array, P.a, ea), b: etendre(Int32Array, P.b, eb),
    nom: etendre(Int32Array, P.nom, en), noms: noms.length ? P.noms.concat(noms) : P.noms,
    base: P, nBase: P.n, mBase: P.m, km,
  };

  // Grille : chaque cellule s'accroche au nœud le plus proche, nouveaux nœuds compris
  const g = P.grille;
  S.grille = { ...g, pro: { tout: g.pro.tout.slice(), amenage: g.pro.amenage.slice() }, dist: { tout: g.dist.tout.slice(), amenage: g.dist.amenage.slice() } };
  if (nl.length) {
    const max = Math.max(CFG.accesMaxM.tout, CFG.accesMaxM.amenage);
    const mx = g.dlon * R.kx, my = g.dlat * R.ky, ri = Math.ceil(max / mx) + 1, rj = Math.ceil(max / my) + 1;
    for (let q = 0; q < nl.length; q++) {
      const v = P.n + q, lon = nl[q], lat = nb[q];
      const ic = Math.floor((lon - g.w) / g.dlon), jc = Math.floor((g.n - lat) / g.dlat);
      for (let j = Math.max(0, jc - rj); j <= Math.min(g.ny - 1, jc + rj); j++) {
        const dy = (g.n - (j + 0.5) * g.dlat - lat) * R.ky;
        for (let i = Math.max(0, ic - ri); i <= Math.min(g.nx - 1, ic + ri); i++) {
          const d = Math.hypot((g.w + (i + 0.5) * g.dlon - lon) * R.kx, dy);
          if (d > max) continue;
          const k = j * g.nx + i;
          for (const mode of ["tout", "amenage"]) {
            if (d < S.grille.dist[mode][k]) { S.grille.dist[mode][k] = d; S.grille.pro[mode][k] = v; }
          }
        }
      }
    }
  }
  return S;
}

// ---------- Accroche à l'écran : point le plus proche du réseau (existant ou projeté) ----------
// Renvoie { lon, lat, d } ; colle au nœud si le point est proche d'un bout d'arête.
function accrocheReseau(S, idx, lon, lat, maxM) {
  const R = repere(S.base || S), X = x => (x - R.lon0) * R.kx, Y = y => (y - R.lat0) * R.ky;
  const px = X(lon), py = Y(lat), dl = maxM / R.kx, dt = maxM / R.ky;
  let mieux = null;
  const essayer = e => {
    const a = S.a[e], b = S.b[e];
    const ax = X(S.lon[a]), ay = Y(S.lat[a]), dx = X(S.lon[b]) - ax, dy = Y(S.lat[b]) - ay, L2 = dx * dx + dy * dy;
    let t = L2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) : 0;
    const L = Math.sqrt(L2);
    if (t * L < maxM * 0.4) t = 0; else if ((1 - t) * L < maxM * 0.4) t = 1;   // préférer les nœuds
    const d = Math.hypot(ax + t * dx - px, ay + t * dy - py);
    if (d <= maxM && (!mieux || d < mieux.d)) mieux = { d, lon: S.lon[a] + t * (S.lon[b] - S.lon[a]), lat: S.lat[a] + t * (S.lat[b] - S.lat[a]) };
  };
  aretesDans(idx, S.base || S, lon - dl, lat - dt, lon + dl, lat + dt, essayer);
  for (let e = S.mBase || S.m; e < S.m; e++) essayer(e);
  return mieux;
}

// ---------- Temps projet : jamais pire que l'existant ----------
// Départ : le nœud le plus proche dans le graphe projet ET celui de l'existant (multi-source), pour qu'un tronçon
// isolé tout près du départ ne dégrade pas le résultat. Cellules : meilleur des deux rattachements.
function departProjet(P, Gb, S, Gs, lon, lat) {
  const max = CFG.accesMaxM[Gs.mode], srcs = [];
  for (const acc of [accrocher(S, Gs, lon, lat), accrocher(P, Gb, lon, lat)]) {
    if (acc && acc.distM <= max) srcs.push([acc.noeud, accesSecondes(acc.distM), acc.distM]);
  }
  if (!srcs.length) return null;
  srcs.sort((x, y) => x[1] - y[1]);
  const res = dijkstra(Gs, srcs[0][0], srcs[0][1], srcs.slice(1));
  return { res, acc: { noeud: srcs[0][0], distM: srcs[0][2] } };
}

function tempsGrilleProjet(P, S, mode, res) {
  const g = S.grille, gb = P.grille, N = g.nx * g.ny, out = new Float32Array(N), max = CFG.accesMaxM[mode];
  const pro = g.pro[mode], dd = g.dist[mode], proB = gb.pro[mode], ddB = gb.dist[mode];
  for (let k = 0; k < N; k++) {
    if (!g.dedans[k]) { out[k] = NaN; continue; }
    let t = Infinity;
    if (pro[k] >= 0 && dd[k] <= max && isFinite(res.dist[pro[k]])) t = res.dist[pro[k]] + accesSecondes(dd[k]);
    if (proB[k] >= 0 && ddB[k] <= max && isFinite(res.dist[proB[k]])) t = Math.min(t, res.dist[proB[k]] + accesSecondes(ddB[k]));
    out[k] = t / 60;
  }
  return out;
}

// Meilleure arrivée (graphe projet ou rattachement existant) : { noeud, distM, total } ou null
function arriveeProjet(P, Gb, S, Gs, res, lon, lat) {
  const max = CFG.accesMaxM[Gs.mode];
  let mieux = null;
  for (const acc of [accrocher(S, Gs, lon, lat), accrocher(P, Gb, lon, lat)]) {
    if (!acc || acc.distM > max || !isFinite(res.dist[acc.noeud])) continue;
    const total = res.dist[acc.noeud] + accesSecondes(acc.distM);
    if (!mieux || total < mieux.total) mieux = { ...acc, total };
  }
  return mieux;
}

// ---------- Lambert 93 → WGS84 (import de couches QGIS non reprojetées) ----------
function lambert93VersWgs84(x, y) {
  const n = 0.7256077650532670, C = 11754255.426096, xs = 700000, ys = 12655612.049876, e = 0.0818191910428158;
  const dx = x - xs, dy = y - ys, Rr = Math.hypot(dx, dy), gamma = Math.atan(dx / -dy);
  const lon = 3 + gamma / n * 180 / Math.PI, L = -Math.log(Rr / C) / n;
  let phi = 2 * Math.atan(Math.exp(L)) - Math.PI / 2;
  for (let i = 0; i < 12; i++) {
    const s = e * Math.sin(phi);
    phi = 2 * Math.atan(Math.pow((1 + s) / (1 - s), e / 2) * Math.exp(L)) - Math.PI / 2;
  }
  return [lon, phi * 180 / Math.PI];
}

// GeoJSON (LineString / MultiLineString, WGS84 ou Lambert 93) → tronçons
function tracesDepuisGeoJSON(gj) {
  const feats = gj.type === "FeatureCollection" ? gj.features : gj.type === "Feature" ? [gj] : [{ type: "Feature", geometry: gj, properties: {} }];
  const crs = JSON.stringify(gj.crs || "");
  const out = [];
  for (const f of feats) {
    const geom = f && f.geometry;
    if (!geom) continue;
    const lignes = geom.type === "LineString" ? [geom.coordinates] : geom.type === "MultiLineString" ? geom.coordinates : [];
    const p = f.properties || {};
    const type = TYPES_PROJET[p.type] ? p.type : /bande/i.test(JSON.stringify(p)) ? "bande" : /stabilis|grave|calcaire/i.test(JSON.stringify(p)) ? "stabilise" : "piste";
    for (const l of lignes) {
      if (!l || l.length < 2) continue;
      const l93 = /2154/.test(crs) || Math.abs(l[0][0]) > 1000;
      out.push({ type, nom: p.nom || p.name || p.libelle || "", coords: l.map(([x, y]) => (l93 ? lambert93VersWgs84(x, y) : [x, y])).map(([x, y]) => [+x.toFixed(6), +y.toFixed(6)]) });
    }
  }
  return out;
}

// ---------- Encodage compact des tronçons pour le lien de partage (polyline Google, 1e-5) ----------
function encoderLigne(coords) {
  let s = "", plat = 0, plon = 0;
  const enc = v => { v = v < 0 ? ~(v << 1) : v << 1; while (v >= 0x20) { s += String.fromCharCode((0x20 | (v & 0x1f)) + 63); v >>= 5; } s += String.fromCharCode(v + 63); };
  for (const [lon, lat] of coords) {
    const la = Math.round(lat * 1e5), lo = Math.round(lon * 1e5);
    enc(la - plat); enc(lo - plon); plat = la; plon = lo;
  }
  return s;
}
function decoderLigne(s) {
  const out = [];
  let i = 0, lat = 0, lon = 0;
  const dec = () => { let r = 0, sh = 0, b; do { b = s.charCodeAt(i++) - 63; r |= (b & 0x1f) << sh; sh += 5; } while (b >= 0x20 && i < s.length + 1); return r & 1 ? ~(r >> 1) : r >> 1; };
  while (i < s.length) { lat += dec(); lon += dec(); out.push([lon / 1e5, lat / 1e5]); }
  return out;
}

if (typeof module !== "undefined") {
  module.exports = { TYPES_PROJET, SCN, indexerAretes, construireScenario, accrocheReseau, departProjet, tempsGrilleProjet, arriveeProjet, lambert93VersWgs84, tracesDepuisGeoJSON, encoderLigne, decoderLigne };
}

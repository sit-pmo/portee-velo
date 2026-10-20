// tuiles-worker.js : chargement des fenêtres de tuiles hors du fil principal (19/10/2026).
// Lecture, décompression, décodage et recollage des tuiles, graphe « tout » et sa contraction (coeur.js), puis
// graphe « sans les routes » : à Paris en 7 × 7, ~1 s puis ~1,5 s qui gelaient la page à chaque changement de
// fenêtre. Les tableaux sont transférés à la page sans copie (ils quittent le Worker).
// Messages reçus : { type: "init", index, dossier, rayon, capacite, grille }, puis { id, type: "fenetre", lon, lat },
// { id, type: "couloir", lon1, lat1, lon2, lat2, demiLargeurKm }, { type: "precharger", lon, lat }.
// Réponses : { id, P, G, partielle, manquantes, nb, centre, signature } (P absent : « meme » = même fenêtre que la
// réponse précédente, que la page a gardée), puis les suites : { type: "suite", signature, reseau } (lignes des pistes et
// des bandes, à plat), { type: "suite", signature, rapide } (contraction du graphe « tout »), { type: "amenage", signature, G }.
importScripts("coeur.js");

let chargeur = null, dossier = "", derniere = null;

// Tous les ArrayBuffer d'un objet (une fois chacun) : liste de transfert de postMessage
function tampons(o, vus = new Set(), out = []) {
  if (!o || typeof o !== "object" || vus.has(o)) return out;
  vus.add(o);
  if (ArrayBuffer.isView(o)) { if (!vus.has(o.buffer)) { vus.add(o.buffer); out.push(o.buffer); } return out; }
  for (const k of Object.keys(o)) tampons(o[k], vus, out);
  return out;
}

// Graphe d'un mode réduit à ce qu'il faut pour le transférer (sans fonction)
const pourEnvoi = G => G && { ...G };
const envoyer = m => postMessage(m, tampons(m));
const pause = () => new Promise(r => setTimeout(r, 0));   // laisse passer une nouvelle demande entre deux étapes

// Suites d'une fenêtre, après sa réponse : lignes du réseau (dessin), contraction du graphe « tout » (survol),
// graphe « sans les routes » et sa contraction. Envoyées une à une ; abandonnées si une autre fenêtre est
// arrivée entre-temps (elle passe devant). Elles travaillent sur des copies : le transfert a emporté P et G.
let suite = null;
async function poursuivre(s) {
  for (const etape of s.etapes) {
    await pause();
    if (suite !== s) return;
    etape();
  }
  if (suite === s) suite = null;
}

async function assembler(id, cles, avecGraphes, extra = {}) {
  const tuiles = await Promise.all(cles.map(c => chargeur.tuile(c)));
  const manquantes = cles.filter((_, i) => tuiles[i] === null), recues = tuiles.filter(t => t !== null);
  chargeur.elaguer(new Set(cles));
  const signature = cles.filter((_, i) => tuiles[i] !== null).join(" ");
  const base = { id, partielle: manquantes.length > 0, manquantes, nb: cles.length, signature, ...extra };
  // Même ensemble de tuiles que la dernière réponse : la page réutilise sa fenêtre (les tableaux sont chez elle)
  if (signature === derniere) { postMessage({ ...base, meme: true }); return; }
  derniere = signature;
  if (!recues.length) { postMessage({ ...base, P: null }); return; }
  const P = recollerTuiles(recues);
  delete P.idGlobal;                             // fonction : ne passe pas par postMessage (inutilisée par la page)
  if (!avecGraphes) { envoyer({ ...base, P }); return; }
  const G = construireGraphe(P, "tout");
  // Copies pour les suites (arêtes, coordonnées, grille, graphe « tout »), faites avant que le transfert ne vide P et G
  const Pc = { n: P.n, m: P.m, a: P.a.slice(), b: P.b.slice(), lm: P.lm.slice(), cls: P.cls.slice(), sens: P.sens.slice(),
    vit: P.vit.slice(), rd: P.rd.slice(), nat: P.nat ? P.nat.slice() : undefined, lat: P.lat.slice(), lon: P.lon.slice(),
    grille: { pro: { tout: P.grille.pro.tout.slice(), amenage: P.grille.pro.amenage.slice() } } };
  const Gc = { n: G.n, nArcs: G.nArcs, off: G.off.slice(), cible: G.cible.slice(), poids: G.poids.slice() };
  envoyer({ ...base, P, G: pourEnvoi(G) });
  const s = { signature, etapes: [
    () => envoyer({ type: "suite", signature, reseau: [emballerLignes(lignesReseau(Pc, 0)), emballerLignes(lignesReseau(Pc, 1))] }),
    () => envoyer({ type: "suite", signature, rapide: contracter(Gc, noeudsCites(Pc, "tout")) }),
    () => { const Ga = construireGraphe(Pc, "amenage"); graphRapide(Ga, Pc); delete Pc._contractions; envoyer({ type: "amenage", signature, G: pourEnvoi(Ga) }); },
  ] };
  suite = s;
  poursuivre(s);
}

onmessage = async e => {
  const m = e.data;
  try {
    if (m.type === "init") {
      definirGrille(m.grille);
      dossier = m.dossier;
      chargeur = new ChargeurTuiles({ index: m.index, rayon: m.rayon, capacite: m.capacite,
        lire: (tx, ty) => fetch(`${dossier}${tx}_${ty}.bin`).then(r => {
          if (!r.ok) throw new Error(`tuile ${tx}_${ty} : ${r.status}`);
          return r.arrayBuffer();
        }) });
    } else if (m.type === "fenetre") {
      const [tx0, ty0] = chargeur.tuileDe(m.lon, m.lat);
      await assembler(m.id, chargeur.voisines(tx0, ty0, chargeur.rayon), true, { centre: [tx0, ty0] });
    } else if (m.type === "couloir") {
      // Itinéraire lointain : même choix de tuiles que ChargeurTuiles.couloir, sans graphe (la page le construit)
      let cles = null;
      const orig = chargeur.assembler;
      chargeur.assembler = async c => { cles = c; return {}; };
      try { await chargeur.couloir(m.lon1, m.lat1, m.lon2, m.lat2, m.demiLargeurKm); } finally { chargeur.assembler = orig; }
      await assembler(m.id, cles, false);
    } else if (m.type === "precharger") {
      chargeur.precharger(m.lon, m.lat);
    }
  } catch (err) {
    if (m.id != null) postMessage({ id: m.id, erreur: String(err && err.message || err) });
  }
};

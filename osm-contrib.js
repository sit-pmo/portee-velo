// Contribution à OpenStreetMap (17/10/2026, niveaux 1 et 2 de la boucle de contribution) : depuis un clic sur la
// carte, montrer ce que la carte a compris de la voie (classe, vitesse, sens lus dans la tuile), ce qu'en dit OSM
// aujourd'hui (tags lus à la demande sur l'API d'OSM, Overpass en secours), des indices de lecture, et ouvrir iD ou une
// note OSM à cet endroit.
// Rien n'est jamais écrit dans OSM depuis la page : on renvoie toujours vers un humain dans iD ou sur osm.org.
//
// Les tuiles ne portent pas l'identifiant des voies OSM : la voie est retrouvée au clic (voies highway autour de
// l'arête), puis choisie par la distance de ses deux extrémités à la géométrie de chaque voie.
//
// Source (20/10/2026) : l'API principale d'OSM (api.openstreetmap.org/api/0.6/map.json, petit carré autour de l'arête).
// Les instances publiques d'Overpass refusaient trop souvent (« rien ne s'affiche », retour d'Aurélien) ; elles restent
// en secours. Les extrémités d'une arête sont des nœuds OSM (la tuile ne découpe que les segments de plus de 2 km) :
// le carré qui les englobe contient donc des nœuds de la voie, et /map renvoie toute voie qui a un nœud dans le carré.
//
// Partie pure (lecture des tags, choix de la voie, liens) testée sous Node : chaine/test/contrib.test.js.
// Partie page (installerContribOsm) : Leaflet et le DOM, appelée par index.html.
const CONTRIB_OSM = (() => {
  const HASHTAG = "porteevelo";
  // Instances publiques d'Overpass, essayées dans l'ordre (CORS ouvert) : elles refusent parfois (429 : trop de
  // demandes depuis la même adresse, fréquent derrière un opérateur mobile ; 504 : serveur chargé), d'où les secours
  const OVERPASS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter"];
  const API_OSM = "https://api.openstreetmap.org/api/0.6/map.json";
  const MARGE_API_M = 25;             // marge du carré autour des extrémités de l'arête
  const DELAI_MS = 8000;              // par service : au-delà, on passe au suivant
  const RAYON_M = 20;                 // voies cherchées autour du milieu de l'arête
  const RAD = Math.PI / 180, R_TERRE = 6371008.8;

  // Règles de classer() (build_graph_velo.py) et classer_fin() (chaine/tuiler.py), reprises pour l'explication
  // seulement : le calcul, lui, lit la classe dans la tuile. Une divergence ne fausse donc que le texte d'aide.
  const HIGHWAYS = new Set(["cycleway", "path", "footway", "pedestrian", "track", "bridleway", "living_street", "residential",
    "unclassified", "road", "service", "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link"]);
  const CHEMINS = new Set(["path", "footway", "pedestrian", "track", "bridleway"]);
  const VELO_OK = new Set(["yes", "designated", "permissive", "destination"]);
  const VELO_INTERDIT = new Set(["no", "dismount", "use_sidepath"]);
  const VELO_OK_TRUNK = new Set(["yes", "designated", "permissive"]);
  const SERVICES_EXCLUS = new Set(["driveway", "parking_aisle", "drive-through", "emergency_access"]);
  const AMENAGEMENTS = new Set(["lane", "track", "shared_busway"]);
  const CLES_CYCLEWAY = ["cycleway", "cycleway:both", "cycleway:left", "cycleway:right"];
  const CLASSE_FINE = { unclassified: 5, track: 6, path: 6, footway: 6, pedestrian: 6, bridleway: 6, tertiary: 7, tertiary_link: 7 };
  const VITESSE_REVETEMENT = { asphalt: 20, paved: 20, concrete: 20, chipseal: 19, "concrete:plates": 19, "concrete:lanes": 19,
    paving_stones: 17, sett: 15, cobblestone: 13, unhewn_cobblestone: 11, metal: 16, wood: 16, compacted: 17, fine_gravel: 16,
    unpaved: 13, gravel: 13, ground: 13, dirt: 13, earth: 13, pebblestone: 12, grass: 9, woodchips: 9, sand: 8, mud: 7 };
  const VITESSE_TRACKTYPE = { grade1: 20, grade2: 17, grade3: 13, grade4: 11, grade5: 9 };
  // Tags montrés dans la fiche, dans cet ordre (les autres restent visibles sur osm.org)
  const TAGS_UTILES = ["highway", "name", "ref", "bicycle", "cycleway", "cycleway:both", "cycleway:right", "cycleway:left",
    "segregated", "foot", "surface", "smoothness", "tracktype", "oneway", "oneway:bicycle", "junction", "access", "service",
    "bicycle_road", "cyclestreet", "maxspeed", "bridge", "tunnel", "layer"];

  const amenage = t => t.bicycle_road === "yes" || t.cyclestreet === "yes" || CLES_CYCLEWAY.some(k => AMENAGEMENTS.has(t[k]));
  const sensDe = t => {
    if (t["oneway:bicycle"] === "no") return 0;
    if (["cycleway", "cycleway:left", "cycleway:right"].some(k => String(t[k] || "").startsWith("opposite"))) return 0;
    let ow = t["oneway:bicycle"] || t.oneway;
    if (ow === undefined && ["roundabout", "circular"].includes(t.junction)) ow = "yes";
    return ["yes", "1", "true"].includes(ow) ? 1 : ow === "-1" ? 2 : 0;
  };
  const vitesseDe = (t, hw, classe) => {
    if (t.surface in VITESSE_REVETEMENT) return VITESSE_REVETEMENT[t.surface];
    if (hw === "track") return VITESSE_TRACKTYPE[t.tracktype] || 15;
    if (hw === "path" || hw === "bridleway") return classe === 0 ? 17 : 13;
    if (["footway", "pedestrian", "living_street"].includes(hw)) return 16;
    return 20;
  };

  // Tags d'une voie → { classe, vitesse, sens } comme au tuilage, ou { ecartee: raison } ; plus des indices : ce qu'un
  // contributeur peut vérifier sur le terrain et corriger (texte court, tag en cause)
  function lireTags(tags) {
    let t = { ...tags };
    const hw0 = t.highway, indices = [];
    if ((hw0 === "trunk" || hw0 === "trunk_link") && VELO_OK_TRUNK.has(t.bicycle)) t.highway = hw0 === "trunk" ? "primary" : "primary_link";
    const hw = t.highway, velo = t.bicycle;
    let ecartee = null;
    if (!hw) ecartee = "ce n'est pas une voie (pas de tag highway)";
    else if (hw === "motorway" || hw === "motorway_link") ecartee = "autoroute : jamais empruntée";
    else if (hw === "trunk" || hw === "trunk_link") ecartee = "voie express non ouverte au vélo (bicycle=yes absent)";
    else if (!HIGHWAYS.has(hw)) ecartee = `highway=${hw} n'est pas une voie prise en compte`;
    else if (VELO_INTERDIT.has(velo)) ecartee = `vélo interdit ou à pied (bicycle=${velo})`;
    else if (t.area === "yes") ecartee = "surface (area=yes), pas une voie";
    else if (["no", "private"].includes(t.access) && !VELO_OK.has(velo)) ecartee = `accès ${t.access === "no" ? "interdit" : "privé"} (access=${t.access})`;
    else if (hw === "service" && SERVICES_EXCLUS.has(t.service)) ecartee = `voie de service exclue (service=${t.service})`;
    else if (["footway", "pedestrian", "bridleway"].includes(hw) && !VELO_OK.has(velo))
      ecartee = `${hw === "bridleway" ? "chemin équestre" : "voie piétonne"} sans autorisation vélo (bicycle=yes ou designated absent)`;
    if (ecartee) {
      if (["footway", "pedestrian"].includes(hw) && !velo)
        indices.push({ tag: "bicycle", texte: "Si les vélos y sont autorisés (panneau, voie partagée), ajoutez bicycle=yes ; si c'est une piste ou une voie verte, bicycle=designated." });
      return { ecartee, indices };
    }
    let classe;
    if (hw === "cycleway" || (CHEMINS.has(hw) && velo === "designated")) classe = 0;
    else if (amenage(t)) classe = 1;
    else if (hw === "primary" || hw === "primary_link") classe = 4;
    else if (hw === "secondary" || hw === "secondary_link") classe = 3;
    else classe = CLASSE_FINE[hw] ?? 2;
    const vitesse = vitesseDe(t, hw, classe), sens = sensDe(t);

    if (!t.surface && !(hw === "track" && t.tracktype))
      indices.push({ tag: "surface", texte: `Revêtement non renseigné : vitesse par défaut (${vitesse} km/h). Ajoutez surface=asphalt, compacted, fine_gravel…` });
    if (classe >= 2 && classe !== 6 && !CLES_CYCLEWAY.some(k => k in t) && !t["cycleway:right:lane"])
      indices.push({ tag: "cycleway", texte: "Aucun aménagement cyclable décrit sur cette route. S'il y a une bande ou une piste sur le terrain, décrivez-la (cycleway:right=lane, track…) ou dessinez la piste à part (highway=cycleway)." });
    if (classe === 6 && hw === "path" && !velo)
      indices.push({ tag: "bicycle", texte: "Chemin sans indication vélo : compté comme simple chemin. Si c'est une piste ou une voie verte, ajoutez bicycle=designated." });
    if (sens && t["oneway:bicycle"] === undefined && classe >= 2)
      indices.push({ tag: "oneway:bicycle", texte: "Sens unique aussi pour les vélos. S'il existe un double-sens cyclable (panneau « sauf vélos »), ajoutez oneway:bicycle=no." });
    if (hw === "cycleway" && t.oneway === "yes" && t["oneway:bicycle"] === undefined)
      indices.push({ tag: "oneway", texte: "Piste à sens unique : vérifiez que c'est bien le cas sur le terrain." });
    return { classe, vitesse, sens, indices };
  }

  // Distance (m) du point p à la ligne pts ([lon, lat]), dans le plan local de p
  function distanceLigne([lon, lat], pts) {
    const kx = Math.cos(lat * RAD) * RAD * R_TERRE, ky = RAD * R_TERRE;
    let d = Infinity;
    for (let i = 1; i < pts.length; i++) {
      const ax = (pts[i - 1][0] - lon) * kx, ay = (pts[i - 1][1] - lat) * ky, bx = (pts[i][0] - lon) * kx, by = (pts[i][1] - lat) * ky;
      const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
      const s = l2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / l2)) : 0;
      d = Math.min(d, Math.hypot(ax + s * dx, ay + s * dy));
    }
    if (pts.length === 1) d = Math.hypot((pts[0][0] - lon) * kx, (pts[0][1] - lat) * ky);
    return d;
  }

  // Voie OSM (élément Overpass « out geom ») qui porte l'arête : la plus proche de ses deux extrémités et de son milieu
  // (une rue qui croise l'arête à une extrémité n'est proche que d'un point). null si aucune à moins de RAYON_M.
  function choisirVoie(elements, extremites) {
    const [p, q] = extremites, mil = [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
    let meilleure = null, dMin = Infinity;
    for (const el of elements || []) {
      if (el.type !== "way" || !el.geometry?.length) continue;
      const pts = el.geometry.map(g => [g.lon, g.lat]);
      const d = distanceLigne(p, pts) + distanceLigne(q, pts) + distanceLigne(mil, pts);
      if (d < dMin) { dMin = d; meilleure = el; }
    }
    return meilleure && dMin / 3 <= RAYON_M ? meilleure : null;
  }

  // Carré (lon/lat) qui englobe les extrémités de l'arête, élargi de margeM mètres : « ouest,sud,est,nord » pour /map
  function boiteAutour(extremites, margeM = MARGE_API_M) {
    const lat0 = (extremites[0][1] + extremites[1][1]) / 2;
    const dLat = margeM / (RAD * R_TERRE), dLon = dLat / Math.cos(lat0 * RAD);
    const lons = extremites.map(p => p[0]), lats = extremites.map(p => p[1]);
    return [Math.min(...lons) - dLon, Math.min(...lats) - dLat, Math.max(...lons) + dLon, Math.max(...lats) + dLat]
      .map(v => v.toFixed(7)).join(",");
  }
  const urlApi = extremites => `${API_OSM}?bbox=${boiteAutour(extremites)}`;

  // Réponse de /map.json → voies highway au format d'Overpass « out tags geom meta » (geometry = [{lat, lon}]),
  // pour que choisirVoie et la fiche lisent l'une ou l'autre source
  function voiesDeLApi(json) {
    const noeuds = new Map();
    for (const e of json?.elements || []) if (e.type === "node") noeuds.set(e.id, e);
    const voies = [];
    for (const e of json?.elements || []) {
      if (e.type !== "way" || !e.tags?.highway) continue;
      const geometry = (e.nodes || []).map(id => noeuds.get(id)).filter(Boolean).map(n => ({ lat: n.lat, lon: n.lon }));
      voies.push({ type: "way", id: e.id, tags: e.tags, timestamp: e.timestamp, version: e.version, geometry });
    }
    return voies;
  }

  // Voies autour de l'arête : API d'OSM, puis les instances d'Overpass, chacune bornée à DELAI_MS. Renvoie
  // { elements, source } ; l'erreur dit si un service a refusé pour trop de demandes. lire = fetch (remplacé en test).
  async function lireVoies(extremites, signal, lire = fetch) {
    let tropDeDemandes = false;
    const lat = (extremites[0][1] + extremites[1][1]) / 2, lon = (extremites[0][0] + extremites[1][0]) / 2;
    const essais = [
      { source: "api", url: urlApi(extremites), options: {}, lireJson: voiesDeLApi },
      ...OVERPASS.map(url => ({ source: "overpass", url, options: { method: "POST", body: new URLSearchParams({ data: requete(lat, lon) }) },
        lireJson: j => j.elements || [] })),
    ];
    for (const { source, url, options, lireJson } of essais) {
      const essai = new AbortController(), minuteur = setTimeout(() => essai.abort(), DELAI_MS);
      const relayer = () => essai.abort();
      signal?.addEventListener("abort", relayer);
      try {
        const r = await lire(url, { ...options, signal: essai.signal });
        if (r.ok) return { elements: lireJson(await r.json()), source };
        if (r.status === 429 || r.status === 509) tropDeDemandes = true;   // 509 : quota de l'API d'OSM dépassé
      } catch (e) { if (signal?.aborted) throw e; }   // délai dépassé ou réseau : service suivant
      finally { clearTimeout(minuteur); signal?.removeEventListener("abort", relayer); }
    }
    const err = new Error("OpenStreetMap injoignable"); err.tropDeDemandes = tropDeDemandes;
    throw err;
  }

  const requete = (lat, lon) =>
    `[out:json][timeout:15];way(around:${RAYON_M},${lat.toFixed(6)},${lon.toFixed(6)})[highway];out tags geom meta;`;

  // Liens : iD sur la voie (ou au point), voie et historique sur osm.org, nouvelle note au point
  const lienId = ({ lat, lon, way }) =>
    `https://www.openstreetmap.org/edit?editor=id${way ? `&way=${way}` : ""}#map=19/${lat.toFixed(6)}/${lon.toFixed(6)}&hashtags=${HASHTAG}`;
  const lienVoie = way => `https://www.openstreetmap.org/way/${way}`;
  const lienHistorique = way => `https://www.openstreetmap.org/way/${way}/history`;
  const lienNote = (lat, lon) => `https://www.openstreetmap.org/note/new#map=19/${lat.toFixed(6)}/${lon.toFixed(6)}`;

  // Texte proposé pour une note (l'utilisateur le relit et le complète sur osm.org)
  function texteNote({ voie, carte }) {
    const nom = voie?.tags?.name ? ` (${voie.tags.name})` : "";
    const lu = carte ? ` La carte « À portée de vélo » la lit comme : ${carte}.` : "";
    return voie
      ? `Voie ${voie.id}${nom} : à vérifier pour le vélo.${lu} Sur le terrain : … #${HASHTAG}`
      : `Une voie utilisable à vélo semble manquer ici (piste, passage, raccord).${lu} Sur le terrain : … #${HASHTAG}`;
  }

  // Voie modifiée dans OSM après la date des données de la carte ? (timestamp Overpass ISO, genere « AAAA-MM-JJ HH:MM »)
  const modifieeDepuis = (timestamp, genere) => !!(timestamp && genere) && new Date(timestamp) > new Date(genere.replace(" ", "T") + "Z");

  return { HASHTAG, API_OSM, OVERPASS, DELAI_MS, TAGS_UTILES, lireTags, choisirVoie, distanceLigne, requete, boiteAutour,
    urlApi, voiesDeLApi, lireVoies, lienId, lienVoie, lienHistorique, lienNote, texteNote, modifieeDepuis };
})();

// ---------- Partie page : outil « Vérifier dans OSM » ----------
// o = { carte, $, vue: () => P de la fenêtre, CLASSES, genere, avantDebut: () => void (arrête les autres outils),
//       suivre, etat, areteProche }
function installerContribOsm(o) {
  const { carte, $ } = o, C = CONTRIB_OSM;
  const insp = { actif: false, suiviAvant: false, ctrl: null, cache: new Map() };
  const couche = L.layerGroup().addTo(carte);
  const fiche = $("#osm-fiche"), bouton = $("#osm-verifier");
  const SENS = ["double sens", "sens unique", "sens unique"];   // le sens du tracé des arêtes peut être retourné au tuilage

  const el = (tag, attrs = {}, ...enfants) => {   // DOM sans innerHTML : les tags OSM sont du texte venu d'ailleurs
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) if (k === "texte") e.textContent = v; else e.setAttribute(k, v);
    for (const x of enfants) if (x) e.append(x);
    return e;
  };
  const remplir = (cible, ...enfants) => cible.replaceChildren(...enfants.filter(Boolean));
  const lien = (href, texte) => el("a", { href, target: "_blank", rel: "noopener", texte });

  function commencer() {
    if (insp.actif) return;
    o.avantDebut();
    insp.actif = true; insp.suiviAvant = o.etat.suivre;
    o.suivre(false);
    bouton.setAttribute("aria-pressed", "true");
    fiche.hidden = false;
    fiche.replaceChildren(el("p", { class: "note", texte: "Cliquez sur une voie (ou là où une voie manque) pour voir comment OpenStreetMap la décrit. Échap ou le bouton pour arrêter." }));
    carte.getContainer().style.cursor = "help";
  }
  function arreter() {
    if (!insp.actif) return;
    insp.actif = false;
    insp.ctrl?.abort();
    bouton.setAttribute("aria-pressed", "false");
    fiche.hidden = true; fiche.replaceChildren();
    couche.clearLayers();
    carte.getContainer().style.cursor = "";
    o.suivre(insp.suiviAvant);
  }

  // Ce que dit la tuile de l'arête e (texte court, aussi repris dans la note)
  function lectureCarte(P, e) {
    const morceaux = [o.CLASSES[P.cls[e]]?.nom || "voie", `${P.vit[e]} km/h`, SENS[P.sens[e]] || ""];
    if (P.rd[e]) morceaux.push("départementale"); else if (P.nat && P.nat[e]) morceaux.push("nationale");
    return morceaux.filter(Boolean).join(", ");
  }

  // surVoie : une voie est là (dans nos données) mais sans fiche OSM : iD s'ouvre simplement à cet endroit
  function boutons(lat, lon, voie, carteTexte, surVoie = false) {
    const note = C.texteNote({ voie, carte: carteTexte });
    // Un seul geste pour la note : osm.org n'accepte pas de texte pré-rempli dans l'URL (17/10/2026), donc le clic
    // copie le texte suggéré puis ouvre le formulaire (lien normal : pas de bloqueur de fenêtres). La copie est lancée
    // avant l'ouverture de l'onglet, tant que la page a encore le focus.
    const ajouterNote = lien(C.lienNote(lat, lon), "Ajouter une note sur OSM (texte copié, à coller)");
    const aide = el("p", { class: "note", texte: "Le texte suggéré est copié : collez-le (Ctrl+V ou appui long) dans la note, puis complétez-le avec ce que vous voyez sur le terrain." });
    aide.hidden = true;
    ajouterNote.addEventListener("click", () => {
      aide.hidden = false;
      navigator.clipboard?.writeText(note).catch(() => { aide.textContent = `Copie impossible : recopiez ce texte dans la note. ${note}`; });
      if (!navigator.clipboard) aide.textContent = `Recopiez ce texte dans la note : ${note}`;
    });
    return el("div", { class: "osm-actions" },
      lien(C.lienId({ lat, lon, way: voie?.id }), voie ? "Modifier cette voie dans OSM (iD)" : surVoie ? "Ouvrir cet endroit dans OSM (iD)" : "Ajouter la voie dans OSM (iD)"),
      voie ? lien(C.lienHistorique(voie.id), "Historique de la voie") : null,
      ajouterNote, aide);
  }

  async function clic(ll) {
    const P = o.vue();
    couche.clearLayers();
    insp.ctrl?.abort();
    const tolM = carte.distance(ll, carte.containerPointToLatLng(carte.latLngToContainerPoint(ll).add([12, 0])));
    const e = P ? o.areteProche(P, ll.lng, ll.lat, Math.max(5, tolM), 0) : -1;
    if (e < 0) {
      // Rien dans nos données : une voie manque peut-être dans OSM, ou elle y est mais n'est pas utilisable à vélo
      L.circleMarker(ll, { pane: "trace", radius: 7, color: "#7B2CBF", weight: 2, fill: false, interactive: false }).addTo(couche);
      remplir(fiche,
        el("h3", { texte: "Aucune voie cyclable ici dans nos données" }),
        el("p", { class: "note", texte: "Si une piste, un passage ou un raccord existe sur le terrain, il manque peut-être dans OpenStreetMap, ou il y est sans autorisation vélo. Vous pouvez l'ajouter ou le signaler :" }),
        boutons(ll.lat, ll.lng, null, null));
      return;
    }
    const a = P.a[e], b = P.b[e], ext = [[P.lon[a], P.lat[a]], [P.lon[b], P.lat[b]]];
    L.polyline(ext.map(([x, y]) => [y, x]), { pane: "trace", color: "#fff", weight: 10, opacity: .9, interactive: false }).addTo(couche);
    L.polyline(ext.map(([x, y]) => [y, x]), { pane: "trace", color: "#7B2CBF", weight: 5, interactive: false }).addTo(couche);
    const lat = (ext[0][1] + ext[1][1]) / 2, lon = (ext[0][0] + ext[1][0]) / 2, carteTexte = lectureCarte(P, e);
    const zoneOsm = el("div", {}, el("p", { class: "note", texte: "Lecture d'OpenStreetMap…" }));
    remplir(fiche,
      el("h3", { texte: "Sur cette carte" }),
      el("p", { class: "osm-lu", texte: `${carteTexte} (données du ${o.genere.slice(0, 10).split("-").reverse().join("/")})` }),
      zoneOsm);

    const cle = `${ext[0]}|${ext[1]}`;
    let elements = insp.cache.get(cle);
    if (!elements) {
      const ctrl = insp.ctrl = new AbortController();
      try { elements = (await C.lireVoies(ext, ctrl.signal)).elements; }
      catch (err) {
        if (err.name === "AbortError") return;
        const reessayer = el("button", { type: "button", class: "lien-action", texte: "Réessayer" });
        reessayer.addEventListener("click", () => clic(ll));
        remplir(zoneOsm, el("p", { class: "note", texte: err.tropDeDemandes
          ? "OpenStreetMap limite les demandes trop rapprochées : réessayez dans une minute. Les liens restent utilisables :"
          : "OpenStreetMap ne répond pas pour l'instant. Les liens restent utilisables :" }),
          reessayer, boutons(lat, lon, null, carteTexte, true));
        return;
      }
      if (ctrl.signal.aborted) return;
      insp.cache.set(cle, elements);
    }
    const voie = C.choisirVoie(elements, ext);
    if (!voie) {
      remplir(zoneOsm, el("p", { class: "note", texte: "Cette voie n'est plus dans OpenStreetMap à cet endroit (supprimée ou déplacée depuis nos données)." }), boutons(lat, lon, null, carteTexte, true));
      return;
    }
    // Voie OSM entière en surbrillance, sous l'arête
    L.polyline(voie.geometry.map(g => [g.lat, g.lon]), { pane: "trace", color: "#7B2CBF", weight: 3, opacity: .6, dashArray: "6 5", interactive: false }).addTo(couche);
    const tags = voie.tags || {}, lu = C.lireTags(tags);
    const table = el("table", { class: "osm-tags" });
    for (const k of C.TAGS_UTILES) if (k in tags) table.append(el("tr", {}, el("th", { texte: k }), el("td", { texte: tags[k] })));
    const autres = Object.keys(tags).length - table.rows.length;
    const lecture = lu.ecartee ? `écartée du calcul : ${lu.ecartee}` : `${o.CLASSES[lu.classe]?.nom}, ${lu.vitesse} km/h, ${SENS[lu.sens]}`;
    const recente = C.modifieeDepuis(voie.timestamp, o.genere);
    remplir(zoneOsm, 
      el("h3", {}, "Dans OpenStreetMap : ", lien(C.lienVoie(voie.id), `voie ${voie.id}`)),
      recente ? el("p", { class: "osm-recent", texte: `Modifiée le ${new Date(voie.timestamp).toLocaleDateString("fr-FR")}, après nos données : la carte en tiendra compte à la prochaine mise à jour.` }) : null,
      table,
      autres > 0 ? el("p", { class: "note", texte: `${autres} autre${autres > 1 ? "s" : ""} tag${autres > 1 ? "s" : ""} sur osm.org.` }) : null,
      el("p", { class: "osm-lu", texte: `Lue aujourd'hui comme : ${lecture}` }),
      lu.indices.length ? el("ul", { class: "osm-indices" }, ...lu.indices.map(i => el("li", {}, el("code", { texte: i.tag }), " ", i.texte))) : null,
      boutons(lat, lon, voie, carteTexte));
  }

  bouton.addEventListener("click", () => { if (!insp.actif && typeof mesurer === "function") mesurer("verifier-voie-osm"); insp.actif ? arreter() : commencer(); });
  document.addEventListener("keydown", ev => { if (insp.actif && ev.key === "Escape") arreter(); });
  return { get actif() { return insp.actif; }, clic, arreter };
}

if (typeof module !== "undefined") module.exports = CONTRIB_OSM;

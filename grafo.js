// tuconstitucion.mx — Grafo de relaciones entre artículos
// Construye el modelo de datos (nodos + aristas) para el grafo 3D.
// Las aristas de "referencia cruzada" se extraen del texto oficial en tiempo real,
// así que el grafo crece automáticamente conforme se incorporan más artículos.

(function () {
  const EST = window.CPEUM_ESTRUCTURA || [];
  const TEMAS = window.CPEUM_TEMAS || {};
  const TEXTOS = window.CPEUM_TEXTOS || {};

  // ---- Paleta por Título (agrupación estructural) ----
  const COLOR_TITULO = [
    '#006847', // I  — Derechos humanos
    '#0a7d5a', // II — Soberanía / territorio
    '#c8a25f', // III— Poderes de la Unión
    '#b5872f', // IV — Responsabilidades
    '#ce1126', // V  — Estados y CDMX
    '#8c1c2b', // VI — Trabajo
    '#5b6f8c', // VII— Prevenciones generales
    '#4a4a8a', // VIII—Reformas
    '#6b5d3f'  // IX — Inviolabilidad
  ];

  // Índice artículo -> {titIdx, tituloNombre, capNombre}
  function indiceEstructura() {
    const idx = {};
    EST.forEach((t, ti) => {
      t.capitulos.forEach((c) => {
        for (let n = c.desde; n <= c.hasta; n++) {
          idx[n] = {
            titIdx: ti,
            titulo: t.titulo.split('—')[0].trim(),
            tituloFull: t.titulo,
            capitulo: c.nombre || ''
          };
        }
      });
    });
    return idx;
  }

  // ---- Temas transversales (juicio editorial; cruzan títulos) ----
  // Cada hub agrupa artículos por materia, más allá de su ubicación estructural.
  const TEMAS_HUB = [
    { id: 'H_libertades', label: 'Libertades fundamentales', emoji: '🕊️', color: '#2e9e6b',
      arts: [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 24] },
    { id: 'H_justicia', label: 'Justicia y debido proceso', emoji: '⚖️', color: '#4472b8',
      arts: [13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 94, 97, 100, 102, 103, 105, 107] },
    { id: 'H_democracia', label: 'Democracia y participación', emoji: '🗳️', color: '#c8a25f',
      arts: [34, 35, 36, 39, 40, 41, 116, 122, 135] },
    { id: 'H_economia', label: 'Economía y propiedad', emoji: '💰', color: '#b5872f',
      arts: [25, 26, 27, 28, 73, 123, 131] },
    { id: 'H_poderes', label: 'Poderes de la Unión', emoji: '🏛️', color: '#9a5fb0',
      arts: [49, 50, 73, 80, 89, 90, 94, 102] },
    { id: 'H_federalismo', label: 'Federalismo y territorio', emoji: '🗺️', color: '#ce1126',
      arts: [40, 42, 43, 115, 116, 117, 121, 122, 124] },
    { id: 'H_sociales', label: 'Derechos sociales', emoji: '🤝', color: '#0a7d5a',
      arts: [2, 3, 4, 27, 123] }
  ];

  // ---- Extracción de referencias cruzadas desde el texto ----
  // "en términos del artículo 73", "los artículos 21 y 73", "artículo 123, apartado A"...
  const RE_REF = /\bart[íi]culos?\s+(\d+[ºo]?\.?(?:\s*(?:,|y|e)\s*\d+[ºo]?\.?)*)/gi;

  function extraerReferencias() {
    const edges = {}; // "a->b" -> peso
    Object.keys(TEXTOS).forEach((k) => {
      const n = +k;
      const texto = TEXTOS[k];
      texto.split('\n').forEach((linea) => {
        if (/\bDOF\b/.test(linea)) return; // ignora notas de reforma
        let m;
        const r = new RegExp(RE_REF.source, 'gi');
        while ((m = r.exec(linea))) {
          const nums = (m[1].match(/\d+/g) || []).map(Number)
            .filter((x) => x >= 1 && x <= 136 && x !== n);
          nums.forEach((x) => {
            const key = n + '->' + x;
            edges[key] = (edges[key] || 0) + 1;
          });
        }
      });
    });
    return edges;
  }

  // ---- Construcción del grafo ----
  // opts: { temas:bool, pendientes:bool, propsByArt:{n:count} }
  function construir(opts) {
    opts = opts || {};
    const mostrarTemas = opts.temas !== false;
    const mostrarPend = opts.pendientes !== false;
    const props = opts.propsByArt || {};
    const idx = indiceEstructura();

    const refEdges = extraerReferencias();
    const grado = {}; // conexiones por artículo (para tamaño)
    Object.keys(refEdges).forEach((k) => {
      const [a, b] = k.split('->').map(Number);
      grado[a] = (grado[a] || 0) + 1;
      grado[b] = (grado[b] || 0) + 1;
    });

    // ¿qué artículos participan en alguna arista de referencia?
    const enRef = new Set();
    Object.keys(refEdges).forEach((k) => {
      const [a, b] = k.split('->').map(Number);
      enRef.add(a); enRef.add(b);
    });

    const nodes = [];
    const links = [];

    // Nodos de artículo
    for (let n = 1; n <= 136; n++) {
      const tiene = !!TEXTOS[n];
      // filtro de pendientes: siempre incluir si tiene texto, está en una
      // referencia, o pertenece a un tema; ocultar el resto si mostrarPend=false
      const relevante = tiene || enRef.has(n) ||
        (mostrarTemas && TEMAS_HUB.some((h) => h.arts.includes(n)));
      if (!mostrarPend && !relevante) continue;

      const info = idx[n] || { titIdx: 0, titulo: '', capitulo: '' };
      const g = grado[n] || 0;
      const p = props[n] || 0;
      nodes.push({
        id: 'a' + n,
        art: n,
        tipo: 'articulo',
        tiene: tiene,
        label: 'Art. ' + n,
        tema: TEMAS[n] || '',
        titulo: info.titulo,
        capitulo: info.capitulo,
        color: COLOR_TITULO[info.titIdx] || '#57606a',
        val: 1.4 + g * 0.9 + (tiene ? 1.1 : 0) + p * 0.7,
        grado: g,
        props: p
      });
    }

    const existe = new Set(nodes.map((x) => x.id));

    // Aristas de referencia cruzada (dirigidas, sólidas)
    Object.keys(refEdges).forEach((k) => {
      const [a, b] = k.split('->').map(Number);
      const sa = 'a' + a, sb = 'a' + b;
      if (existe.has(sa) && existe.has(sb)) {
        links.push({ source: sa, target: sb, tipo: 'ref', peso: refEdges[k] });
      }
    });

    // Nodos de tema + aristas temáticas (tenues)
    if (mostrarTemas) {
      TEMAS_HUB.forEach((h) => {
        const conectados = h.arts.filter((n) => existe.has('a' + n));
        if (!conectados.length) return;
        nodes.push({
          id: h.id,
          tipo: 'tema',
          label: h.emoji + ' ' + h.label,
          temaLabel: h.label,
          color: h.color,
          val: 6 + conectados.length * 0.5
        });
        conectados.forEach((n) => {
          links.push({ source: h.id, target: 'a' + n, tipo: 'tema' });
        });
      });
    }

    return {
      nodes: nodes,
      links: links,
      stats: {
        articulos: nodes.filter((x) => x.tipo === 'articulo').length,
        conTexto: nodes.filter((x) => x.tipo === 'articulo' && x.tiene).length,
        refs: links.filter((l) => l.tipo === 'ref').length,
        temas: TEMAS_HUB.length
      }
    };
  }

  // Relaciones de un artículo concreto (para el panel en la página del artículo)
  function relacionesDe(n) {
    const refEdges = extraerReferencias();
    const salientes = [], entrantes = [];
    Object.keys(refEdges).forEach((k) => {
      const [a, b] = k.split('->').map(Number);
      if (a === n) salientes.push(b);
      if (b === n) entrantes.push(a);
    });
    const temas = TEMAS_HUB.filter((h) => h.arts.includes(n)).map((h) => h.label);
    return {
      salientes: [...new Set(salientes)].sort((x, y) => x - y),
      entrantes: [...new Set(entrantes)].sort((x, y) => x - y),
      temas: temas
    };
  }

  window.CPEUM_GRAFO = {
    construir: construir,
    relacionesDe: relacionesDe,
    TEMAS_HUB: TEMAS_HUB,
    COLOR_TITULO: COLOR_TITULO
  };
})();

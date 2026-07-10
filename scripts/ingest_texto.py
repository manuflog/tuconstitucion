#!/usr/bin/env python3
"""
tuConstitución.mx — Fase 2 de la pirámide: texto completo de leyes prioritarias.

Fuente oficial: PDFs de texto consolidado vigente de LeyesBiblio (los mismos
pdf_url que ya viven en tc_instruments, ingeridos por ingest_instrumentos.py).

Qué hace, por cada ley del lote:
  1. Descarga el PDF oficial y extrae el texto (PyMuPDF).
  2. Limpia mobiliario de página de forma GENÉRICA (líneas repetidas ≥5 veces:
     encabezado con el nombre de la ley, "CÁMARA DE DIPUTADOS...", paginación).
  3. Reconstruye párrafos y parte en artículos. El parser tolera claves
     reales de leyes federales: "Artículo 12 Bis.", "Artículo 47 Bis 1.",
     "Artículo 17-A.-", "Artículo 4o.-C.", "(Se deroga)".
  4. Se detiene en los TRANSITORIOS (después de ellos solo hay decretos).
  5. Hash-diff contra tc_law_articles y upsert solo de lo que cambió.
  6. Marca tc_instruments.articles_ingested=true + content_hash del PDF.

Uso:
  python ingest_texto.py                      # lote prioritario (PRIORIDAD)
  python ingest_texto.py --slugs cnpp,cpf     # leyes específicas
  python ingest_texto.py --selftest
  python ingest_texto.py --dry-run --slugs lamp

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_KEY (service role).
"""
import os
import re
import sys
import json
import hashlib
import argparse
import datetime as dt
from collections import Counter

# Lote prioritario: máximo impacto ciudadano + citas desde la CPEUM.
PRIORIDAD = ["cnpp", "cpf", "lamp", "lft", "lgs", "ccf", "cff", "lge",
             "lgdnna", "lan"]

SUFIJOS = (r"Bis|Ter|Qu[áa]ter|Quinquies|Quintus|Sexies|Sextus|Septies|"
           r"Septimus|Octies|Octavus|Nonies|Decies|Undecies|Duodecies")

# Encabezados de artículo. Dos estilos reales en LeyesBiblio:
#  - ordinal:  "Artículo 1o. ...", "Artículo 4o.- ...", "Artículo 4o.-C."
#    (el punto del ordinal ya cuenta como puntuación de cierre)
#  - estándar: "Artículo 10.", "Artículo 17-A.-", "Artículo 47 Bis 1."
# "Artículo"/"ARTÍCULO" capitalizados: las referencias en texto corrido usan
# minúscula y no arrancan línea tras el reflow.
_ART = r"^(?:Artículo|ART[ÍI]CULO)\s+"
RE_ART_ORD = re.compile(
    _ART + r"(\d{1,2})o\.(?:\s*-\s*([A-ZÑ])(?=[\.\s]))?")
RE_ART_STD = re.compile(
    _ART + r"(\d{1,4})"
    r"(?:\s*-\s*([A-ZÑ]))?"
    r"(?:\s+(" + SUFIJOS + r"))?"
    r"(?:\s+(\d{1,2}))?"
    r"\s*[\.\-–—:]", )


def match_art(ln: str):
    """→ (num:int, key:str) si la línea es encabezado de artículo, si no None."""
    m = RE_ART_ORD.match(ln)
    if m:
        key = m.group(1) + (("-" + m.group(2)) if m.group(2) else "")
        return int(m.group(1)), key
    m = RE_ART_STD.match(ln)
    if m:
        key = m.group(1)
        if m.group(2):
            key += "-" + m.group(2)
        if m.group(3):
            key += " " + m.group(3)
        if m.group(4):
            key += " " + m.group(4)
        return int(m.group(1)), key
    return None

RE_TITULO = re.compile(r"^T[ÍI]TULO\s+", re.I)
RE_CAP = re.compile(r"^CAP[ÍI]TULO\s+", re.I)
RE_SECCION = re.compile(r"^SECCI[ÓO]N\s+", re.I)
RE_LIBRO = re.compile(r"^LIBRO\s+", re.I)
RE_TRANS = re.compile(r"^(ART[ÍI]CULOS?\s+)?TRANSITORIOS?\b", re.I)
RE_ULTIMA = re.compile(r"Últimas?\s+Reformas?\s+(?:publicadas\s+)?DOF\s+(\d{2}-\d{2}-\d{4})", re.I)
RE_FRAC = re.compile(r"^(X{0,3}(IX|IV|V?I{0,3})|[A-Z])\.\s")
RE_INC = re.compile(r"^[a-z]\)\s")
ANOT = re.compile(
    r"^(Denominación|Párrafo|Artículo|Fracción|Inciso|Apartado|Base|Numeral|"
    r"Sección|Capítulo|Título|Libro|Reforma|Adición|Adicionado|Fe de erratas|"
    r"Reformado|Derogado|Reubicado|Recorrido|Se reforma|Se adiciona)\b.*"
    r"(reformad|adicionad|derogad|reubicad|recorrid|DOF|Fe de erratas)", re.I)


def _hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode("utf-8")).hexdigest()[:16]


def clean_lines(raw: str):
    """Mobiliario genérico: líneas cortas repetidas ≥5 veces (encabezados de
    página con el nombre de la ley, cámara, secretarías) + paginación."""
    lines = [l.strip() for l in raw.split("\n")]
    freq = Counter(l for l in lines if l and len(l) < 120 and not match_art(l))
    furniture = {l for l, n in freq.items() if n >= 5 and not RE_FRAC.match(l)
                 and not RE_INC.match(l) and not l[0:1].islower()
                 and not RE_TRANS.match(l)}
    out = []
    for ln in lines:
        if not ln:
            continue
        if ln in furniture:
            continue
        if re.fullmatch(r"\d+\s+de\s+\d+", ln):        # "37 de 310"
            continue
        if RE_ULTIMA.search(ln) and len(ln) < 60:
            continue
        out.append(ln)
    return out


def is_marker(ln: str) -> bool:
    return bool(RE_TITULO.match(ln) or RE_CAP.match(ln) or RE_SECCION.match(ln)
                or RE_LIBRO.match(ln) or match_art(ln) or RE_TRANS.match(ln)
                or ANOT.match(ln))


def reflow(lines):
    """Reconstruye párrafos partidos por ancho de columna (misma heurística
    probada en ingest_cpeum.py)."""
    corridas = [len(l) for l in lines if not is_marker(l)]
    colw = max(corridas) if corridas else 100
    paras, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            paras.append(buf.strip())
        buf = ""

    i = 0
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ANOT.match(ln) and not match_art(ln):
            flush(); paras.append(ln); i += 1; continue
        if (RE_TITULO.match(ln) or RE_CAP.match(ln) or RE_SECCION.match(ln)
                or RE_LIBRO.match(ln) or RE_TRANS.match(ln)):
            flush(); paras.append(ln); i += 1; continue
        if match_art(ln) and buf.strip():
            flush()
        starts_item = bool(RE_FRAC.match(ln) or RE_INC.match(ln))
        if starts_item and buf.strip():
            flush()
        buf = (buf + " " + ln).strip() if buf else ln
        ends_sentence = bool(re.search(r"[\.\:;]$", ln))
        short = len(ln) <= colw - 4
        next_is_break = is_marker(nxt) or bool(RE_FRAC.match(nxt) or RE_INC.match(nxt))
        if next_is_break or (ends_sentence and short):
            flush()
        i += 1
    flush()
    return paras


def split_articles(paras):
    """→ lista ordenada de dicts {art_key, art_sort, division, texto}."""
    arts = []
    cur = None
    body = []
    division = {"libro": "", "titulo": "", "capitulo": "", "seccion": ""}
    maxnum = 0
    seen_keys = set()

    def commit():
        if cur is not None and body:
            arts.append({"art_key": cur, "art_sort": len(arts) + 1,
                         "division": {k: v for k, v in division.items() if v},
                         "texto": "\n".join(body).strip()})

    for p in paras:
        if RE_TRANS.match(p) and len(arts) >= 5:
            commit()
            return arts
        if RE_LIBRO.match(p):
            division = {"libro": p, "titulo": "", "capitulo": "", "seccion": ""}
            continue
        if RE_TITULO.match(p):
            division = {**division, "titulo": p, "capitulo": "", "seccion": ""}
            continue
        if RE_CAP.match(p):
            division = {**division, "capitulo": p, "seccion": ""}
            continue
        if RE_SECCION.match(p):
            division = {**division, "seccion": p}
            continue
        m = match_art(p)
        if m:
            num, key = m
            # encabezado real: número no-decreciente, salto acotado, clave nueva.
            # (las referencias "Artículo 14." en texto corrido no arrancan
            # párrafo tras el reflow, y suelen ser regresivas o repetidas)
            if key not in seen_keys and (maxnum == 0 or (num >= maxnum and num - maxnum <= 25)):
                commit()
                cur = key
                seen_keys.add(key)
                maxnum = num
                body = [p]
            elif cur is not None:
                body.append(p)
            continue
        if cur is not None:
            body.append(p)
    commit()
    return arts


def parse_pdf_text(raw: str):
    ultima = ""
    m = RE_ULTIMA.search(raw)
    if m:
        ultima = m.group(1)
    return split_articles(reflow(clean_lines(raw))), ultima


# ------------------------- Fetch + Supabase -------------------------

def fetch_pdf(url):
    import requests
    r = requests.get(url, timeout=120,
                     headers={"User-Agent": "tuconstitucion.mx ingesta"})
    r.raise_for_status()
    return r.content


def pdf_to_text(data: bytes) -> str:
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json"}


def process_law(base, inst, dry=False, force=False):
    import requests
    slug, iid, pdf_url = inst["slug"], inst["id"], inst["pdf_url"]
    print(f"→ {slug}: {inst['name'][:60]}")
    data = fetch_pdf(pdf_url)
    pdf_hash = hashlib.sha256(data).hexdigest()[:16]
    if not force and inst.get("content_hash") == pdf_hash:
        print(f"  sin cambios (hash {pdf_hash}), omitida")
        return 0
    arts, ultima = parse_pdf_text(pdf_to_text(data))
    if len(arts) < 10:
        print(f"  ERROR: solo {len(arts)} artículos parseados — formato inesperado, omitida")
        return -1
    print(f"  {len(arts)} artículos (última reforma PDF: {ultima or 'n/d'})")
    if dry:
        for a in arts[:3] + arts[-2:]:
            print(f"    Art. {a['art_key']}: {a['texto'][:70]}...")
        return len(arts)

    # hash-diff contra lo existente
    r = requests.get(f"{base}/rest/v1/tc_law_articles?instrument_id=eq.{iid}"
                     f"&select=art_key,hash", headers=sb_headers(), timeout=60)
    r.raise_for_status()
    existing = {row["art_key"]: row["hash"] for row in r.json()}
    payload = []
    for a in arts:
        h = _hash(a["texto"])
        if existing.get(a["art_key"]) == h:
            continue
        payload.append({"instrument_id": iid, "art_key": a["art_key"],
                        "art_sort": a["art_sort"], "division": a["division"],
                        "texto": a["texto"], "hash": h,
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    for i in range(0, len(payload), 200):
        resp = requests.post(
            base + "/rest/v1/tc_law_articles?on_conflict=instrument_id,art_key",
            headers={**sb_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload[i:i + 200], timeout=120)
        resp.raise_for_status()
    # marcar instrumento
    requests.patch(
        f"{base}/rest/v1/tc_instruments?id=eq.{iid}",
        headers={**sb_headers(), "Prefer": "return=minimal"},
        json={"articles_ingested": True, "content_hash": pdf_hash,
              "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        timeout=60).raise_for_status()
    print(f"  upsert: {len(payload)} artículos nuevos/cambiados")
    return len(arts)


# ------------------------- selftest -------------------------

SAMPLE = """LEY DEMO DEL PUEBLO
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
Última Reforma DOF 01-02-2026
TÍTULO PRIMERO
Disposiciones Generales
CAPÍTULO I
Del Objeto
Artículo 1. Esta ley es de orden público. Las referencias al
Artículo 99 de otra norma no abren artículo nuevo porque son regresivas
luego de avanzar.
Artículo 2. Segundo artículo con
línea partida por columna.
Artículo 2 Bis. Variante bis.
Artículo 2 Bis 1. Variante bis numerada.
Artículo 3.- (Se deroga)
Artículo 4o.- Con ordinal y guión.
Artículo 4o.-C. Ordinal con letra.
ARTÍCULO 4 Bis. Encabezado en mayúsculas.
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
LEY DEMO DEL PUEBLO
Artículo 5o. Ordinal con punto y espacio, estilo Ley de Amparo.
Artículo 6. En términos del Artículo 1 de esta Ley se aplica lo siguiente:
I. Primera fracción;
II. Segunda fracción.
Artículo 17-A.- Clave con letra.
TRANSITORIOS
Primero. Este decreto entrará en vigor al día siguiente.
Artículo 90. Esto es de un decreto y NO debe capturarse.
LEY DEMO DEL PUEBLO
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
LEY DEMO DEL PUEBLO
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
LEY DEMO DEL PUEBLO
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
"""


def selftest():
    arts, ultima = parse_pdf_text(SAMPLE)
    keys = [a["art_key"] for a in arts]
    assert keys == ["1", "2", "2 Bis", "2 Bis 1", "3", "4", "4-C", "4 Bis",
                    "5", "6", "17-A"], keys
    assert ultima == "01-02-2026", ultima
    a1 = arts[0]
    assert a1["division"]["titulo"].startswith("TÍTULO PRIMERO"), a1["division"]
    assert a1["division"]["capitulo"].startswith("CAPÍTULO I")
    assert "orden público" in a1["texto"]
    # el mobiliario repetido no contamina
    assert all("CÁMARA DE DIPUTADOS" not in a["texto"] for a in arts)
    # transitorios cortan: el "Artículo 90" del decreto no entra... pero aquí
    # solo hay 8 artículos (<5 no aplica el corte temprano): verificar que 90
    # no está porque el corte por TRANS ocurrió con len(arts)>=5.
    assert "90" not in keys
    # la fracción quedó dentro del art. 6
    a6 = next(a for a in arts if a["art_key"] == "6")
    assert "Primera fracción" in a6["texto"]
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slugs", default="", help="coma-separado; default lote PRIORIDAD")
    ap.add_argument("--force", action="store_true", help="reprocesar aunque el PDF no cambió")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    import requests
    base = os.environ["SUPABASE_URL"].rstrip("/")
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] or PRIORIDAD
    r = requests.get(
        f"{base}/rest/v1/tc_instruments?slug=in.({','.join(slugs)})"
        f"&select=id,slug,name,pdf_url,content_hash", headers=sb_headers(), timeout=60)
    r.raise_for_status()
    found = {i["slug"]: i for i in r.json()}
    missing = [s for s in slugs if s not in found]
    if missing:
        print("aviso: slugs no encontrados:", ", ".join(missing))
    fails = 0
    for s in slugs:
        if s not in found:
            continue
        try:
            if process_law(base, found[s], dry=args.dry_run, force=args.force) < 0:
                fails += 1
        except Exception as e:
            print(f"  ERROR {s}: {e}")
            fails += 1
    if fails:
        sys.exit(f"{fails} leyes fallaron")
    print("listo")


if __name__ == "__main__":
    main()

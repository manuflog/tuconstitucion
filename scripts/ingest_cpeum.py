#!/usr/bin/env python3
"""
tuConstitución.mx — Ingesta automática del texto vigente de la CPEUM.

Fuente oficial (texto consolidado vigente): Cámara de Diputados, Leyes Biblio.
  - Texto completo:  https://www.diputados.gob.mx/LeyesBiblio/pdf/CPEUM.pdf
  - Reformas por art: https://www.diputados.gob.mx/LeyesBiblio/ref/cpeum_art.htm

Qué hace:
  1. Descarga el PDF vigente y extrae su texto (PyMuPDF).
  2. Limpia el "mobiliario" de página (encabezados/pies/numeración) y
     reconstruye los párrafos que el PDF parte por ancho de columna.
  3. Parte el texto en artículos (1..136) conservando las anotaciones de
     reforma ("Párrafo reformado DOF ...").
  4. Descarga la página "Reformas por Artículo" y extrae, por artículo, la
     lista de fechas DOF de sus reformas.
  5. Calcula un hash del texto de cada artículo y lo compara con lo que ya
     está en Supabase (tabla tc_articles). Solo actualiza lo que cambió, y
     marca explainer_stale=true en los artículos nuevos o modificados para
     que el generador de explicaciones (Haiku) los vuelva a redactar.

Detección de cambios global: la portada del PDF trae "Últimas Reformas DOF
dd-mm-aaaa". Si esa fecha no cambió desde la última corrida, salimos pronto
(salvo --force).

Uso:
  python ingest_cpeum.py                # ingesta incremental
  python ingest_cpeum.py --force        # reprocesa aunque la fecha no cambie
  python ingest_cpeum.py --selftest     # prueba el parser con una muestra real
  python ingest_cpeum.py --dry-run      # parsea pero no escribe en Supabase

Variables de entorno (para escribir en Supabase):
  SUPABASE_URL          p.ej. https://hvxidkdxlhewliettepl.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (NUNCA la publishable). Secreto de CI.
"""
import os
import re
import sys
import json
import hashlib
import argparse
import datetime as dt

PDF_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/CPEUM.pdf"
ART_URL = "https://www.diputados.gob.mx/LeyesBiblio/ref/cpeum_art.htm"

# --- Marcadores de anotación de reforma (van en su propia línea) ---
ANOT = re.compile(
    r'^(Denominación|Párrafo|Artículo|Fracción|Inciso|Apartado|Base|Numeral|'
    r'Sección|Capítulo|Título|Reforma|Adición|Adicionado|Fe de erratas|'
    r'Reformado|Derogado|Reubicado|Recorrido|Se reforma|Se adiciona)\b.*'
    r'(reformad|adicionad|derogad|reubicad|recorrid|DOF|Fe de erratas)', re.I)

# Encabezados estructurales
RE_TITULO = re.compile(r'^Título\s+[A-ZÁÉÍÓÚ]', re.I)
RE_CAP = re.compile(r'^Capítulo\s+[IVXLC]+', re.I)
RE_SECCION = re.compile(r'^Sección\s+[IVXLC]+', re.I)
# OJO: sensible a mayúsculas a propósito. Los encabezados reales de artículo
# son "Artículo 1o." (capitalizado). Las referencias dentro de decretos usan
# "artículo 3o." (minúscula) o "ARTÍCULO" (mayúsculas): NO deben confundirse.
RE_ART = re.compile(r'^Artículo\s+(\d+)(?:o\.|°|º|\.|\s)')
RE_TRANS = re.compile(r'^(Transitorio|Artículos?\s+Transitorios?|T\s*R\s*A\s*N\s*S\s*I\s*T)', re.I)

# Mobiliario de página que se repite en cada hoja del PDF
FURNITURE = re.compile(
    r'^(CONSTITUCIÓN POLÍTICA DE LOS ESTADOS UNIDOS MEXICANOS|'
    r'CÁMARA DE DIPUTADOS DEL H\. CONGRESO DE LA UNIÓN|'
    r'Secretaría General|Secretaría de Servicios Parlamentarios|'
    r'Últimas Reformas DOF\s+\d{2}-\d{2}-\d{4}|\d+\s+de\s+\d+)\s*$', re.I)

RE_ULTIMA = re.compile(r'Últimas?\s+Reformas?\s+(?:publicadas\s+)?DOF\s+(\d{2}-\d{2}-\d{4})', re.I)
RE_FRAC = re.compile(r'^(X{0,3}(IX|IV|V?I{0,3})|[A-Z])\.\s')   # I. II. ... o A.
RE_INC = re.compile(r'^[a-z]\)\s')                              # a) b) ...


def _hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode("utf-8")).hexdigest()[:16]


def clean_lines(raw: str):
    """Quita mobiliario de página y espacios; devuelve lista de líneas útiles."""
    out = []
    for ln in raw.split("\n"):
        ln = ln.rstrip()
        if not ln.strip():
            continue
        if FURNITURE.match(ln.strip()):
            continue
        out.append(ln.strip())
    return out


def is_marker(ln: str) -> bool:
    return bool(RE_TITULO.match(ln) or RE_CAP.match(ln) or RE_SECCION.match(ln)
                or RE_ART.match(ln) or RE_TRANS.match(ln) or ANOT.match(ln))


def reflow(lines):
    """Reconstruye párrafos partidos por ancho de columna.

    Heurística: unimos líneas de texto corrido en un mismo párrafo hasta que
    (a) la siguiente línea es una anotación o encabezado, o un marcador de
    fracción/inciso; o (b) la línea actual "cierra" (termina en . : ; y es
    más corta que el ancho típico de columna, señal de fin de párrafo).
    Las anotaciones y encabezados quedan en su propia línea.
    """
    # ancho de columna = línea de texto corrido más larga (ignora anotaciones,
    # encabezados y marcadores, que suelen ser cortos).
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
        if ANOT.match(ln):
            flush(); paras.append(ln); i += 1; continue
        if RE_TITULO.match(ln) or RE_CAP.match(ln) or RE_SECCION.match(ln) or RE_TRANS.match(ln):
            flush(); paras.append(ln); i += 1; continue
        # marcador de fracción/inciso arranca párrafo nuevo
        starts_item = bool(RE_FRAC.match(ln) or RE_INC.match(ln))
        if starts_item and buf.strip():
            flush()
        buf = (buf + " " + ln).strip() if buf else ln
        # ¿cierra el párrafo?
        ends_sentence = bool(re.search(r'[\.\:;]$', ln))
        short = len(ln) <= colw - 4
        next_is_break = is_marker(nxt) or bool(RE_FRAC.match(nxt) or RE_INC.match(nxt))
        if next_is_break or (ends_sentence and short):
            flush()
        i += 1
    flush()
    return paras


def split_articles(paras):
    """Convierte la lista de párrafos en {num: texto} y captura título/capítulo."""
    arts, structure = {}, {}
    cur = None
    titulo = capitulo = ""
    body = []
    want_name = None   # 'titulo' | 'capitulo' cuando esperamos su nombre en la línea siguiente
    maxnum = 0         # último número de artículo aceptado (deben ir 1,2,3,… en orden)
    done = False       # true al llegar a los transitorios tras el artículo 136

    def commit():
        if cur is not None:
            arts[cur] = "\n".join(body).strip()
            structure[cur] = (titulo, capitulo)

    for p in paras:
        if done:
            break
        if RE_TITULO.match(p):
            titulo = p; capitulo = ""; want_name = 'titulo'; continue
        if RE_CAP.match(p) or RE_SECCION.match(p):
            capitulo = p; want_name = 'capitulo'; continue
        if RE_TRANS.match(p):
            # los transitorios y decretos de reforma vienen DESPUÉS del art. 136:
            # al toparnos con ellos (ya cerca del final), dejamos de capturar.
            commit()
            if maxnum >= 130:
                done = True
            cur = None; want_name = None; continue
        m = RE_ART.match(p)
        if m:
            num = int(m.group(1))
            if num == maxnum + 1:            # encabezado real de artículo (secuencial)
                commit(); want_name = None
                cur = num; maxnum = num
                body = [p]
            elif cur is not None:            # "Artículo N" fuera de secuencia = texto del artículo actual
                body.append(p)
            continue
        if want_name and cur is None and not ANOT.match(p):
            # nombre del Título/Capítulo (línea de texto que sigue al encabezado)
            if want_name == 'titulo' and 'Título' not in p:
                titulo = f"{titulo} — {p}"
            elif want_name == 'capitulo':
                capitulo = f"{capitulo} — {p}"
            want_name = None
        elif cur is not None:
            body.append(p)
    commit()
    return arts, structure


def parse_pdf_text(raw: str):
    ultima = ""
    m = RE_ULTIMA.search(raw)
    if m:
        ultima = m.group(1)
    lines = clean_lines(raw)
    paras = reflow(lines)
    arts, structure = split_articles(paras)
    return arts, structure, ultima


def parse_reformas(html: str):
    """De cpeum_art.htm saca {num: [fechas DOF]}."""
    reformas = {}
    # bloques tipo: ARTÍCULO 1o. ... DOF 14-08-2001 ... DOF 04-12-2006 ...
    for m in re.finditer(r'ART[IÍ]CULO\s+(\d+)', html, re.I):
        num = int(m.group(1))
        start = m.end()
        nxt = re.search(r'ART[IÍ]CULO\s+\d+', html[start:], re.I)
        chunk = html[start: start + (nxt.start() if nxt else 4000)]
        fechas = re.findall(r'DOF\s+(\d{2}-\d{2}-\d{4})', chunk)
        # dedup preservando orden
        seen, ordered = set(), []
        for f in fechas:
            if f not in seen:
                seen.add(f); ordered.append(f)
        if ordered:
            reformas[num] = ordered
    return reformas


# ------------------------- Fetch + Supabase -------------------------
def fetch_pdf_text(url=PDF_URL):
    import requests, fitz  # PyMuPDF
    data = requests.get(url, timeout=60).content
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text("text") for page in doc)


def fetch_html(url=ART_URL):
    import requests
    r = requests.get(url, timeout=60)
    r.encoding = "ISO-8859-1"
    return r.text


def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}


def sb_current_hashes(base):
    import requests
    r = requests.get(f"{base}/rest/v1/tc_articles?select=article_id,hash",
                     headers=sb_headers(), timeout=60)
    r.raise_for_status()
    return {row["article_id"]: row.get("hash", "") for row in r.json()}


def sb_upsert(base, rows):
    import requests
    r = requests.post(f"{base}/rest/v1/tc_articles?on_conflict=article_id",
                      headers=sb_headers(), data=json.dumps(rows), timeout=120)
    r.raise_for_status()


def build_rows(arts, structure, reformas, ultima_global, prev_hashes):
    rows, changed = [], 0
    for num, texto in sorted(arts.items()):
        if num < 1 or num > 136:
            continue
        h = _hash(texto)
        if prev_hashes.get(num) == h:
            continue  # sin cambios
        changed += 1
        titulo, capitulo = structure.get(num, ("", ""))
        refs = reformas.get(num, [])
        rows.append({
            "article_id": num,
            "numero": re.sub(r'^Artículo\s+', '', texto.split("\n")[0]).split(".")[0][:12],
            "titulo": titulo, "capitulo": capitulo,
            "texto": texto, "reformas": refs,
            "ultima_reforma": refs[-1] if refs else ultima_global,
            "hash": h, "explainer_stale": True,
            "updated_at": dt.datetime.utcnow().isoformat() + "Z",
        })
    return rows, changed


def run(force=False, dry=False):
    raw = fetch_pdf_text()
    arts, structure, ultima = parse_pdf_text(raw)
    print(f"Parseados {len(arts)} artículos. Última reforma global: {ultima}")
    html = fetch_html()
    reformas = parse_reformas(html)
    print(f"Reformas por artículo: {len(reformas)} artículos con historial.")

    if dry:
        print(json.dumps({k: arts[k][:120] for k in list(sorted(arts))[:3]},
                         ensure_ascii=False, indent=2))
        return
    base = os.environ["SUPABASE_URL"].rstrip("/")
    prev = {} if force else sb_current_hashes(base)
    rows, changed = build_rows(arts, structure, reformas, ultima, prev)
    if not rows:
        print("Sin cambios. Nada que actualizar.")
        return
    sb_upsert(base, rows)
    print(f"Actualizados {changed} artículos en Supabase (marcados para reexplicar).")


# ------------------------------ Self-test ------------------------------
SAMPLE = """CONSTITUCIÓN POLÍTICA DE LOS ESTADOS UNIDOS MEXICANOS
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
Secretaría General
Secretaría de Servicios Parlamentarios
Últimas Reformas DOF 02-06-2026
1 de 414
Título Primero
Capítulo I
De los Derechos Humanos y sus Garantías
Denominación del Capítulo reformada DOF 10-06-2011
Artículo 1o. En los Estados Unidos Mexicanos todas las personas gozarán de los derechos humanos
reconocidos en esta Constitución y en los tratados internacionales de los que el Estado Mexicano sea
parte, así como de las garantías para su protección, cuyo ejercicio no podrá restringirse ni suspenderse,
salvo en los casos y bajo las condiciones que esta Constitución establece.
Párrafo reformado DOF 10-06-2011
Está prohibida la esclavitud en los Estados Unidos Mexicanos. Los esclavos del extranjero que entren
al territorio nacional alcanzarán, por este solo hecho, su libertad y la protección de las leyes.
Queda prohibida toda discriminación motivada por origen étnico o nacional, el género, la edad, las
CONSTITUCIÓN POLÍTICA DE LOS ESTADOS UNIDOS MEXICANOS
CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN
Secretaría General
Secretaría de Servicios Parlamentarios
Últimas Reformas DOF 02-06-2026
2 de 414
discapacidades, la condición social, las condiciones de salud, la religión, las opiniones, las preferencias
sexuales, el estado civil o cualquier otra que atente contra la dignidad humana.
Párrafo reformado DOF 04-12-2006, 10-06-2011
Artículo reformado DOF 14-08-2001
Artículo 2o. La Nación Mexicana es única e indivisible, basada en la grandeza de sus pueblos y
culturas.
Párrafo reformado DOF 30-09-2024
Transitorios
Primero. El presente Decreto entrará en vigor al día siguiente de su publicación.
Artículo Segundo. Se reforma el
artículo 1o. de la Constitución Política de los Estados Unidos Mexicanos, para quedar como sigue:
Artículo 27o. Texto de decreto que NO debe convertirse en el artículo 27 real.
"""

SAMPLE_HTML = """
- ARTÍCULO 1o. 1ª Reforma DOF 14-08-2001 2ª Reforma DOF 04-12-2006 3ª Reforma DOF 10-06-2011
- ARTÍCULO 2o. 1ª Reforma DOF 14-08-2001 2ª Reforma DOF 30-09-2024
"""


def selftest():
    arts, structure, ultima = parse_pdf_text(SAMPLE)
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name)
        ok = ok and cond
    check("detecta última reforma global 02-06-2026", ultima == "02-06-2026")
    check("extrae exactamente 2 artículos", set(arts) == {1, 2})
    check("art.1 conserva estructura (Título/Capítulo)",
          structure[1][0].startswith("Título") and "Derechos Humanos" in structure[1][1])
    a1 = arts[1].split("\n")
    check("art.1 primer párrafo reconstruido en una línea",
          a1[0].startswith("Artículo 1o.") and a1[0].endswith("establece."))
    check("art.1 conserva anotación 'Párrafo reformado DOF 10-06-2011'",
          "Párrafo reformado DOF 10-06-2011" in a1)
    check("art.1 separa 'Está prohibida la esclavitud' de 'Queda prohibida...'",
          any(p.startswith("Está prohibida la esclavitud") for p in a1) and
          any(p.startswith("Queda prohibida toda discriminación") for p in a1))
    check("art.1 termina con 'Artículo reformado DOF 14-08-2001'",
          a1[-1] == "Artículo reformado DOF 14-08-2001")
    check("art.2 no absorbe el mobiliario de página",
          not any("CÁMARA DE DIPUTADOS" in p for p in arts[2].split("\n")))
    check("los decretos/transitorios tras los artículos NO crean artículos falsos",
          27 not in arts and 5 not in arts and set(arts) == {1, 2})
    check("art.1 no fue sobrescrito por una referencia de decreto",
          arts[1].split("\n")[-1] == "Artículo reformado DOF 14-08-2001")
    refs = parse_reformas(SAMPLE_HTML)
    check("reformas art.1 = [14-08-2001, 04-12-2006, 10-06-2011]",
          refs.get(1) == ["14-08-2001", "04-12-2006", "10-06-2011"])
    print("\nRESULTADO:", "TODO OK" if ok else "FALLAS DETECTADAS")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    run(force=a.force, dry=a.dry_run)

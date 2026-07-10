#!/usr/bin/env python3
"""
tuConstitución.mx — Fase 1 de la pirámide normativa: catálogo federal.

Fuente oficial: Cámara de Diputados, Leyes Biblio (compilación oficial).
  - Índice de leyes vigentes:  https://www.diputados.gob.mx/LeyesBiblio/index.htm
  - Leyes abrogadas:           https://www.diputados.gob.mx/LeyesBiblio/abroga.htm

Qué hace:
  1. Descarga el índice (ISO-8859-1, se decodifica correctamente) y parsea
     las ~310 leyes vigentes: número de catálogo, nombre oficial, slug,
     fechas DOF (publicación y última reforma) y URLs de PDF/DOC/historial.
  2. Clasifica cada instrumento en la pirámide (ARQUITECTURA.md):
     CÓDIGO Nacional / LEY General|Nacional → nivel 2; CÓDIGO / LEY → nivel 3.
  3. Upsert a Supabase tc_instruments (on_conflict=slug). El texto completo
     de los artículos NO se ingiere aquí (eso es Fase 2).
  4. Relaciones declaradas: si el propio título oficial dice "Reglamentaria
     de los artículos 103 y 107 de la Constitución...", inserta relaciones
     (ley → artículo CPEUM) en tc_relations con method='declared',
     verified=true y el título como evidencia.
  5. Marca status='abrogada' para leyes del catálogo que aparezcan en la
     página oficial de abrogadas.

Uso:
  python ingest_instrumentos.py             # ingesta normal
  python ingest_instrumentos.py --selftest  # prueba parser y clasificación
  python ingest_instrumentos.py --dry-run   # parsea, no escribe en Supabase

Variables de entorno:
  SUPABASE_URL          p.ej. https://hvxidkdxlhewliettepl.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (NUNCA la publishable). Secreto de CI.
"""
import os
import re
import sys
import argparse
import datetime as dt

BASE = "https://www.diputados.gob.mx/LeyesBiblio/"
INDEX_URL = BASE + "index.htm"
ABROGA_URL = BASE + "abroga.htm"

RE_ROW_SPLIT = re.compile(r"<tr>", re.I)
RE_SLUG = re.compile(r'href="ref/([a-z0-9_]+)\.htm"', re.I)
RE_NUM = re.compile(r">0*(\d{1,3})</font>")
RE_TAGS = re.compile(r"<[^>]+>")
RE_DOF = re.compile(r"DOF\s+(\d{2}/\d{2}/\d{4})")
RE_PDF = re.compile(r'href="(pdf/[^"]+\.pdf)"')
RE_DOC = re.compile(r'href="(doc/[^"]+\.doc)"')

# Título autodeclarativo: "...Reglamentaria de los artículos 103 y 107 de la
# Constitución...", "...Reglamentaria del Artículo 5o. Constitucional...",
# "...Reglamentaria del Artículo 27 Constitucional en el Ramo del Petróleo".
RE_REGL = re.compile(
    r"[Rr]eglamentari[ao][^.;]*?[Aa]rt[íi]culos?\s+(.{0,120}?)"
    r"(?:de la\s+Constituci[óo]n|Constitucional)", re.S)
RE_ARTNUM = re.compile(r"\b(\d{1,3})(?:\s*(?:o\.|°|º))?")


def fetch(url: str) -> str:
    import requests
    r = requests.get(url, timeout=60, headers={"User-Agent": "tuconstitucion.mx ingesta"})
    r.raise_for_status()
    # LeyesBiblio sirve ISO-8859-1; windows-1252 es superconjunto seguro.
    return r.content.decode("windows-1252", errors="replace")


def classify(name: str):
    """→ (type, level) según la pirámide de ARQUITECTURA.md"""
    n = name.upper()
    if n.startswith("CÓDIGO"):
        if "NACIONAL" in n:
            return "codigo_nacional", 2
        return "codigo_federal", 3
    if re.match(r"^LEY\s+(GENERAL|NACIONAL)\b", n):
        return "ley_general", 2
    if n.startswith("CONSTITUCIÓN"):
        return "constitucion", 0
    return "ley_federal", 3


def parse_index(html: str):
    """→ lista de dicts, uno por instrumento del catálogo."""
    out, seen = [], set()
    for chunk in RE_ROW_SPLIT.split(html):
        m = RE_SLUG.search(chunk)
        if not m or 'href="pdf/' not in chunk:
            continue
        slug = m.group(1).lower()
        if slug in seen:
            continue
        # nombre = texto dentro del <a href="ref/slug.htm">...</a>
        mn = re.search(r'href="ref/%s\.htm">(.*?)</a>' % re.escape(slug), chunk, re.S | re.I)
        if not mn:
            continue
        name = re.sub(r"\s+", " ", RE_TAGS.sub(" ", mn.group(1))).strip()
        if not name:
            continue
        seen.add(slug)
        num = RE_NUM.search(chunk)
        dates = RE_DOF.findall(chunk)
        pdf = RE_PDF.search(chunk)
        doc = RE_DOC.search(chunk)
        tipo, lvl = classify(name)
        out.append({
            "slug": slug,
            "name": name,
            "type": tipo,
            "level": lvl,
            "jurisdiction": "federal",
            "catalog_number": int(num.group(1)) if num else None,
            "published_dof": _iso(dates[0]) if dates else None,
            "last_reform_dof": _iso(dates[1]) if len(dates) > 1 else None,
            "official_url": BASE + "ref/%s.htm" % slug,
            "reform_history_url": BASE + "ref/%s.htm" % slug,
            "pdf_url": BASE + pdf.group(1) if pdf else None,
            "doc_url": BASE + doc.group(1) if doc else None,
            "status": "vigente",
        })
    return out


def _iso(ddmmyyyy: str) -> str:
    d, m, y = ddmmyyyy.split("/")
    return "%s-%s-%s" % (y, m, d)


def declared_relations(rows):
    """Relaciones (ley → artículo CPEUM) declaradas en el propio título."""
    rels = []
    for r in rows:
        if r["slug"] == "cpeum":
            continue
        m = RE_REGL.search(r["name"])
        if not m:
            continue
        arts = []
        for a in RE_ARTNUM.findall(m.group(1)):
            v = int(a)
            if 1 <= v <= 136 and str(v) not in arts:
                arts.append(str(v))
        for art in arts:
            rels.append({
                "from_slug": r["slug"],
                "to_article": art,
                "rel_type": "reglamenta",
                "method": "declared",
                "verified": True,
                "evidence": r["name"],
            })
    return rels


def parse_abrogadas(html: str):
    """→ set de slugs presentes en la página oficial de abrogadas."""
    return {m.lower() for m in RE_SLUG.findall(html)}


# ------------------------- Supabase -------------------------

def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json"}


def upsert_instruments(base, rows, source_id, retrieved_at):
    import requests
    for r in rows:
        r["source_id"] = source_id
        r["retrieved_at"] = retrieved_at
    resp = requests.post(
        base + "/rest/v1/tc_instruments?on_conflict=slug",
        headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows, timeout=120)
    resp.raise_for_status()


def get_map(base, table, key, val, flt=""):
    import requests
    r = requests.get("%s/rest/v1/%s?select=%s,%s%s" % (base, table, key, val, flt),
                     headers=sb_headers(), timeout=60)
    r.raise_for_status()
    return {row[key]: row[val] for row in r.json()}


def insert_relations(base, rels, slug2id, cpeum_id):
    import requests
    payload = []
    for rel in rels:
        fid = slug2id.get(rel["from_slug"])
        if not fid:
            continue
        payload.append({
            "from_instrument": fid, "from_article": None,
            "to_instrument": cpeum_id, "to_article": rel["to_article"],
            "rel_type": rel["rel_type"], "method": rel["method"],
            "verified": rel["verified"], "evidence": rel["evidence"],
        })
    if not payload:
        return 0
    resp = requests.post(
        base + "/rest/v1/tc_relations?on_conflict=from_instrument,from_article,to_instrument,to_article,rel_type",
        headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload, timeout=120)
    resp.raise_for_status()
    return len(payload)


def mark_abrogadas(base, slugs):
    import requests
    if not slugs:
        return
    lst = ",".join(sorted(slugs))
    requests.patch(
        base + "/rest/v1/tc_instruments?slug=in.(%s)&status=eq.vigente" % lst,
        headers={**sb_headers(), "Prefer": "return=minimal"},
        json={"status": "abrogada"}, timeout=60).raise_for_status()


# ------------------------- selftest -------------------------

SAMPLE = """<tr><td><font>021</font></td><td><b>
<a href="ref/lamp.htm"><font>LEY de Amparo, Reglamentaria de los artículos 103 y 107
 de la Constitución Política de los Estados Unidos Mexicanos</font></a></b>
<p><font>DOF 02/04/2013</font></p></td><td><font>DOF 16/10/2025</font></td>
<td><a href="pdf/LAmp.pdf"><img></a><a href="doc/LAmp.doc"><img></a></td></tr>
<tr><td><font>009</font></td><td><b>
<a href="ref/cnpp.htm"><font>CÓDIGO Nacional de Procedimientos Penales</font></a></b>
<p><font>DOF 05/03/2014</font></p></td><td><font>DOF 28/11/2025</font></td>
<td><a href="pdf/CNPP.pdf"><img></a></td></tr>
<tr><td><font>150</font></td><td>
<a href="ref/lgs.htm"><font>LEY General de Salud</font></a>
<p><font>DOF 07/02/1984</font></p></td><td><font>DOF 14/11/2025</font></td>
<td><a href="pdf/LGS.pdf"><img></a></td></tr>"""


def selftest():
    rows = parse_index(SAMPLE)
    assert len(rows) == 3, rows
    lamp = next(r for r in rows if r["slug"] == "lamp")
    assert lamp["type"] == "ley_federal" and lamp["level"] == 3
    assert lamp["published_dof"] == "2013-04-02"
    assert lamp["last_reform_dof"] == "2025-10-16"
    assert lamp["pdf_url"].endswith("pdf/LAmp.pdf")
    cnpp = next(r for r in rows if r["slug"] == "cnpp")
    assert cnpp["type"] == "codigo_nacional" and cnpp["level"] == 2
    lgs = next(r for r in rows if r["slug"] == "lgs")
    assert lgs["type"] == "ley_general" and lgs["level"] == 2
    rels = declared_relations(rows)
    assert {(r["from_slug"], r["to_article"]) for r in rels} == {("lamp", "103"), ("lamp", "107")}
    assert all(r["method"] == "declared" and r["verified"] for r in rels)
    # título estilo "Artículo 27 Constitucional en el Ramo del Petróleo"
    r27 = declared_relations([{"slug": "lr27p",
        "name": "LEY Reglamentaria del Artículo 27 Constitucional en el Ramo del Petróleo"}])
    assert {(x["from_slug"], x["to_article"]) for x in r27} == {("lr27p", "27")}
    # "Artículo 5o. Constitucional" → 5
    r5 = declared_relations([{"slug": "lr5",
        "name": "LEY Reglamentaria del Artículo 5o. Constitucional, relativo al ejercicio de las profesiones"}])
    assert {(x["from_slug"], x["to_article"]) for x in r5} == {("lr5", "5")}
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    html = fetch(INDEX_URL)
    rows = [r for r in parse_index(html) if r["slug"] != "cpeum"]
    rels = declared_relations(rows)
    print("instrumentos parseados: %d | relaciones declaradas: %d" % (len(rows), len(rels)))
    if len(rows) < 250:
        sys.exit("ERROR: se esperaban ~310 instrumentos, llegaron %d — ¿cambió el formato del índice?" % len(rows))

    try:
        abrogadas = parse_abrogadas(fetch(ABROGA_URL))
    except Exception as e:
        print("aviso: no se pudo leer abrogadas:", e)
        abrogadas = set()
    vigentes = {r["slug"] for r in rows}
    abrogadas -= vigentes  # una ley no puede estar en ambas listas

    if args.dry_run:
        for r in rows[:5]:
            print(r["catalog_number"], r["slug"], "|", r["type"], "|", r["name"][:70])
        for r in rels[:10]:
            print("REL", r["from_slug"], "→ art.", r["to_article"])
        return

    base = os.environ["SUPABASE_URL"].rstrip("/")
    src = get_map(base, "tc_sources", "publisher", "id")
    source_id = next(v for k, v in src.items() if "LeyesBiblio" in k)
    upsert_instruments(base, rows, source_id, dt.datetime.now(dt.timezone.utc).isoformat())
    slug2id = get_map(base, "tc_instruments", "slug", "id")
    n = insert_relations(base, rels, slug2id, slug2id["cpeum"])
    mark_abrogadas(base, abrogadas & set(slug2id))
    print("upsert OK: %d instrumentos, %d relaciones declaradas" % (len(rows), n))


if __name__ == "__main__":
    main()

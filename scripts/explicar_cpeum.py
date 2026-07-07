#!/usr/bin/env python3
"""
tuConstitución.mx — Generación de explicaciones en lenguaje sencillo (Haiku).

Recorre los artículos de tc_articles marcados como explainer_stale=true
(nuevos o cuyo texto cambió tras una reforma) y, con un modelo pequeño
(claude-haiku-4-5), redacta para cada uno:
  - simple:   "¿Qué dice?" en lenguaje llano
  - importa:  "¿Por qué te importa?"
  - terminos: [{term, definicion}] con 0–4 términos clave

Después baja explainer_stale a false. Así, el costo de IA se paga una sola
vez por artículo (y solo se repite cuando el texto cambia), y la explicación
queda cacheada para todos los visitantes.

Este script está DORMIDO hasta que exista el secreto ANTHROPIC_API_KEY.
No maneja la llave en claro: la lee del entorno (secreto de CI).

Uso:
  python explicar_cpeum.py            # procesa hasta --limit pendientes
  python explicar_cpeum.py --limit 20
  python explicar_cpeum.py --all      # todos los pendientes

Entorno:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, ANTHROPIC_API_KEY
"""
import os
import re
import sys
import json
import argparse

MODEL = "claude-haiku-4-5"

SYSTEM = (
    "Eres un divulgador jurídico mexicano. Explicas artículos de la Constitución "
    "(CPEUM) a ciudadanas y ciudadanos SIN formación legal, con calidez, claridad "
    "y total fidelidad al texto. No inventas, no das asesoría legal, no opinas. "
    "Español de México, lenguaje sencillo y concreto."
)

INSTR = (
    "A partir del TEXTO OFICIAL del artículo, responde SOLO con un objeto JSON válido "
    "con esta forma exacta:\n"
    '{{"simple": "...", "importa": "...", '
    '"terminos": [{{"term": "...", "definicion": "..."}}]}}\n\n'
    "- simple: 1 a 3 frases explicando qué dice el artículo, en lenguaje llano.\n"
    "- importa: 1 a 2 frases sobre por qué le importa a una persona común.\n"
    "- terminos: de 0 a 4 términos jurídicos que aparezcan en el texto, cada uno con "
    "una definición breve y sencilla. Si no hay términos difíciles, deja la lista vacía.\n"
    "No incluyas texto fuera del JSON.\n\n"
    "TÍTULO/TEMA: {tema}\n\nTEXTO OFICIAL DEL ARTÍCULO {num}:\n{texto}"
)


def sb_headers():
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def fetch_pending(base, limit):
    import requests
    q = (f"{base}/rest/v1/tc_articles?explainer_stale=eq.true"
         f"&select=article_id,texto,titulo,capitulo&order=article_id.asc&limit={limit}")
    r = requests.get(q, headers=sb_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def update_article(base, num, simple, importa, terminos):
    import requests
    r = requests.patch(
        f"{base}/rest/v1/tc_articles?article_id=eq.{num}",
        headers=sb_headers(),
        data=json.dumps({"simple": simple, "importa": importa,
                         "terminos": terminos, "explainer_stale": False}),
        timeout=60)
    r.raise_for_status()


def explain(client, art):
    txt = art["texto"][:6000]
    tema = art.get("capitulo") or art.get("titulo") or ""
    msg = client.messages.create(
        model=MODEL, max_tokens=700, system=SYSTEM,
        messages=[{"role": "user",
                   "content": INSTR.format(num=art["article_id"], tema=tema, texto=txt)}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r'\{.*\}', raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    terms = data.get("terminos", []) or []
    terms = [{"term": str(t.get("term", ""))[:60],
              "definicion": str(t.get("definicion", ""))[:400]}
             for t in terms if isinstance(t, dict) and t.get("term")][:4]
    return data.get("simple", "").strip(), data.get("importa", "").strip(), terms


def run(limit):
    from anthropic import Anthropic
    base = os.environ["SUPABASE_URL"].rstrip("/")
    client = Anthropic()  # lee ANTHROPIC_API_KEY del entorno
    pend = fetch_pending(base, limit)
    if not pend:
        print("No hay artículos pendientes de explicar.")
        return
    print(f"Explicando {len(pend)} artículo(s) con {MODEL}…")
    done = 0
    for art in pend:
        n = art["article_id"]
        try:
            simple, importa, terms = explain(client, art)
            if not simple:
                print(f"  · Art. {n}: respuesta vacía, se omite."); continue
            update_article(base, n, simple, importa, terms)
            done += 1
            print(f"  ✓ Art. {n} explicado ({len(terms)} términos).")
        except Exception as e:
            print(f"  ✗ Art. {n}: {e}")
    print(f"Listos {done}/{len(pend)}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    run(limit=1000 if a.all else a.limit)

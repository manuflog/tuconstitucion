# Ingesta automática del texto constitucional

Este pipeline mantiene actualizado el texto de la Constitución en la plataforma,
tomándolo de la fuente oficial (Cámara de Diputados) sin trabajo manual.

## Cómo funciona

```
                 (cada lunes / a mano)
  GitHub Actions ───────────────► scripts/ingest_cpeum.py
                                      │  1. descarga CPEUM.pdf (texto vigente)
                                      │  2. lo parsea en 136 artículos
                                      │  3. lee reformas por artículo (cpeum_art.htm)
                                      │  4. hash-diff: ¿qué cambió?
                                      ▼
                              Supabase: tabla tc_articles  ◄── la web lee de aquí
                                      │  (artículos nuevos/cambiados quedan
                                      │   marcados explainer_stale = true)
                 (cuando exista la    ▼
                  llave de IA)   scripts/explicar_cpeum.py
                                      │  Haiku redacta "qué dice / por qué importa /
                                      │  términos" solo para los pendientes
                                      ▼
                              Supabase: tc_articles (simple, importa, terminos)
```

- **Detección de cambios:** la portada del PDF trae `Últimas Reformas DOF dd-mm-aaaa`.
  Si no cambió, la ingesta no hace nada. Si cambió, solo se actualizan los artículos
  cuyo texto realmente difiere (comparación por hash).
- **Costo de IA acotado:** cada artículo se explica una sola vez; solo se vuelve a
  explicar si su texto cambia por una reforma.
- **La web** ya lee de `tc_articles` con respaldo a los archivos estáticos, así que en
  cuanto la tabla se llena, el sitio muestra los 136 artículos sin necesidad de redeploy.

## Puesta en marcha (una sola vez)

1. **Sube estos archivos al repo** `manuflog/tuconstitucion` (conservando las rutas):
   `scripts/ingest_cpeum.py`, `scripts/explicar_cpeum.py`, `scripts/requirements.txt`,
   `.github/workflows/ingest.yml`, `.github/workflows/explicar.yml`.

2. **Agrega los secretos** en GitHub → Settings → Secrets and variables → Actions:
   - `SUPABASE_URL` = `https://hvxidkdxlhewliettepl.supabase.co`
   - `SUPABASE_SERVICE_KEY` = la **service_role key** (Supabase → Project Settings → API).
     ⚠️ Es una llave con permisos de escritura: va SOLO como secreto de GitHub, nunca en el
     código ni en el frontend (el sitio usa la *publishable key*, que es de solo lectura).
   - `ANTHROPIC_API_KEY` = tu llave de Anthropic — **opcional**, solo cuando quieras activar
     las explicaciones automáticas. Sin ella, la ingesta de texto funciona igual y las
     explicaciones simplemente quedan "en camino".

3. **Primera corrida:** GitHub → Actions → *Ingesta CPEUM* → *Run workflow* (con `force = true`
   para llenar los 136 por primera vez). Luego, si pusiste la llave de IA, corre
   *Explicaciones en lenguaje sencillo* con `all = true`.

Después, todo es automático cada lunes.

## Probar el parser sin tocar nada

```bash
python scripts/ingest_cpeum.py --selftest   # valida el parseo con una muestra real
python scripts/ingest_cpeum.py --dry-run    # descarga y parsea, sin escribir en la BD
```

## Fuentes

- Texto vigente (PDF): https://www.diputados.gob.mx/LeyesBiblio/pdf/CPEUM.pdf
- Reformas por artículo: https://www.diputados.gob.mx/LeyesBiblio/ref/cpeum_art.htm
- Publicación oficial de reformas: Diario Oficial de la Federación (https://www.dof.gob.mx)

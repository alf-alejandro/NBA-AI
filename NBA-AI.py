"""
╔══════════════════════════════════════════════════════════════╗
║          SOCCER EDGE ALPHA BOT  v3.0                        ║
║  Detecta oportunidades de valor en Polymarket Soccer        ║
║                                                              ║
║  FÓRMULA SEA (Soccer Edge Alpha):                           ║
║  valor_raw  = 0.55·P_Vegas + 0.30·N_norm + 0.10·R + (±5V) ║
║  (Draw: sin ventaja de localía, promedio de ambos equipos)  ║
║  penalización titulares: -10% si >2 fuera, -15% si ≥4      ║
║  valor_real = normalizado a 100 entre los 3 outcomes        ║
║  SEA        = P_Poly - valor_real                           ║
║                                                              ║
║  RESUMEN FINAL:                                             ║
║  🎰 SCALPING  : SEA ≤ -20 y valor_real ≥ 30               ║
║  🏆 QUIEN GANA: outcome con mayor real_value cuando el gap  ║
║                 entre los outcomes es ≥ REAL_GAP_MIN        ║
║                                                              ║
║  Requiere:                                                   ║
║    pip install requests google-genai                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re

def _cargar_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_cargar_env()
import json
import requests
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from google import genai
from google.genai import types

# ── Configuración ─────────────────────────────────────────────────────────────

GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
SEA_UMBRAL    = 5.0
SCALP_UMBRAL  = 20.0
SCALP_REAL    = 30.0
REAL_GAP_MIN  = 12.0
GEMINI_MODEL  = "gemini-flash-lite-latest"
GEMINI_RUNS   = 5
DIAS_VENTANA  = 7

HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Términos que indican empate en los outcomes de Polymarket
DRAW_TERMS = {"draw", "tie", "empate", "neither", "no winner", "x", "draw/tie"}

# Deportes que NO son soccer (para filtrar falsos positivos)
NON_SOCCER_TAGS = {
    "basketball", "nba", "ncaa", "nhl", "hockey", "nfl", "american-football",
    "baseball", "mlb", "tennis", "golf", "rugby", "cricket", "mma", "ufc",
    "formula-1", "nascar", "volleyball", "handball", "boxing", "euroleague",
}


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — POLYMARKET (Gamma + CLOB)
# ══════════════════════════════════════════════════════════════════════════════

def _debug_evento(e: dict, prefix: str = "") -> None:
    titulo = e.get("title", "?")[:60]
    cat    = e.get("category", "?")
    fecha  = e.get("startDate") or e.get("endDate") or "?"
    print(f"  {prefix}'{titulo}'  cat='{cat}'  fecha={str(fecha)[:10]}")
    for m in e.get("markets", [])[:2]:
        raw = m.get("outcomes", "[]")
        try:
            outs = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            outs = []
        print(f"       outcomes={outs}")


def _fecha_evento(e: dict) -> str:
    for campo in ["startDate", "startTime", "endDate", "endTime"]:
        val = e.get(campo, "")
        if not val:
            continue
        val_str = str(val)
        try:
            dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
        if len(val_str) >= 10 and val_str[4] == "-":
            return val_str[:10]
    return "sin-fecha"


def _tiene_outcomes_soccer(evento: dict) -> bool:
    """
    Acepta eventos con 2 o 3 outcomes que no sean Yes/No puros.
    En Polymarket soccer: ['Team A', 'Draw (Team A vs Team B)', 'Team B']
    o simplemente ['Team A', 'Team B'] sin draw explícito.
    """
    for m in evento.get("markets", []):
        raw = m.get("outcomes", "[]")
        try:
            outs = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            outs = []
        o_lower = [str(o).strip().lower() for o in outs]
        if all(o in {"yes", "no", "true", "false", "up", "down"} for o in o_lower):
            continue
        if len(outs) >= 2:
            return True
    return False


def _es_more_markets(titulo: str) -> bool:
    tl = titulo.lower()
    return "more markets" in tl or "- additional" in tl


def obtener_partidos_hoy() -> list[dict]:
    """
    v3.0: Usa category="Soccer" — la forma correcta y directa.

    Polymarket organiza sus eventos por CATEGORÍA, no por tag_id.
    - NBA usa: category="NBA" (o series_id=10345)
    - Soccer usa: category="Soccer"

    La página polymarket.com/predictions/soccer lo confirma:
    todos los partidos mostrados tienen category='Soccer'.

    También probamos subcategorías de ligas por si acaso.
    """
    fechas_objetivo = [
        (date.today() + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(DIAS_VENTANA)
    ]
    print(f"  📅 Ventana: {fechas_objetivo[0]} → {fechas_objetivo[-1]}")

    todos_eventos: dict[str, dict] = {}

    def agregar(lista: list[dict]) -> None:
        for e in lista:
            eid = e.get("id")
            if eid:
                todos_eventos[eid] = e

    def fetch(params: dict, label: str) -> list[dict]:
        try:
            resp = SESSION.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "closed": "false",
                        "limit": 200, "order": "startDate", "ascending": "true",
                        **params},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data if isinstance(data, list) else data.get("data", [])
            print(f"  🔎 {label}: {len(result)} eventos")
            return result
        except Exception as ex:
            print(f"  ⚠️  {label}: {ex}")
            return []

    # ── Estrategia 1: category=Soccer (forma directa, como NBA usa su categoría) ─
    print("\n  📡 Estrategia 1: category=Soccer")
    agregar(fetch({"category": "Soccer"}, "category=Soccer"))

    # ── Estrategia 2: category=soccer (minúsculas) ────────────────────────────
    if len(todos_eventos) < 3:
        agregar(fetch({"category": "soccer"}, "category=soccer"))

    # ── Estrategia 3: subcategorías de ligas ─────────────────────────────────
    LIGAS = [
        "Premier League", "La Liga", "Champions League", "Serie A",
        "Bundesliga", "Ligue 1", "MLS", "Europa League",
        "Conference League", "Copa del Rey", "FA Cup",
    ]
    if len(todos_eventos) < 3:
        print("\n  📡 Estrategia 3: subcategorías de ligas")
        for liga in LIGAS:
            agregar(fetch({"category": liga}, f"category={liga}"))
            if len(todos_eventos) >= 30:
                break

    # ── Estrategia 4: tag_slug=soccer ────────────────────────────────────────
    if len(todos_eventos) < 3:
        print("\n  📡 Estrategia 4: tag_slug")
        for slug in ["soccer", "football", "premier-league", "la-liga",
                     "champions-league", "serie-a", "bundesliga"]:
            agregar(fetch({"tag_slug": slug}, f"tag_slug={slug}"))
            if len(todos_eventos) >= 30:
                break

    # ── Estrategia 5: paginación general (más nuevos primero) ─────────────────
    if len(todos_eventos) < 3:
        print("\n  📡 Estrategia 5: paginación general (order=id desc)")
        for offset in [0, 200]:
            try:
                resp = SESSION.get(
                    f"{GAMMA_API}/events",
                    params={"order": "id", "ascending": "false",
                            "closed": "false", "active": "true",
                            "limit": 200, "offset": offset},
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data if isinstance(data, list) else data.get("data", [])
                print(f"  🔎 offset={offset}: {len(batch)} eventos")
                agregar(batch)
            except Exception as ex:
                print(f"  ⚠️  offset={offset}: {ex}")
                break

    print(f"\n  📦 Total eventos cargados: {len(todos_eventos)}")
    if not todos_eventos:
        print("  ❌ Sin respuesta de la API.")
        return []

    # ── Debug: muestra los primeros 5 para diagnóstico ────────────────────────
    print("\n  🔍 Muestra:")
    for e in list(todos_eventos.values())[:5]:
        _debug_evento(e)

    # ── Filtrar partidos de soccer con outcomes válidos ────────────────────────
    soccer_eventos = []
    rechazados = []

    for e in todos_eventos.values():
        titulo = e.get("title", "")
        cat    = (e.get("category") or "").lower()

        if _es_more_markets(titulo):
            continue

        # Criterio primario: category contiene "soccer" (Polymarket lo pone explícito)
        es_soccer_cat = "soccer" in cat or "football" in cat

        # Criterio secundario por tags (si la categoría no basta)
        tags = [
            (t.get("slug", "") if isinstance(t, dict) else str(t)).lower()
            for t in (e.get("tags") or [])
        ]
        tiene_bad_tag  = any(bad in " ".join(tags) for bad in NON_SOCCER_TAGS)
        tiene_socc_tag = any(kw in " ".join(tags) for kw in
                             ["soccer", "football", "premier", "liga", "champions",
                              "bundesliga", "serie-a", "ligue", "europa", "mls"])
        es_soccer_tag  = tiene_socc_tag and not tiene_bad_tag

        # Criterio terciario: sufijo " fc" en el título (alta precisión)
        es_soccer_titulo = bool(re.search(r'\bfc\b', titulo.lower()))

        es_soccer = es_soccer_cat or es_soccer_tag or es_soccer_titulo
        tiene_outs = _tiene_outcomes_soccer(e)

        if es_soccer and tiene_outs:
            soccer_eventos.append(e)
        else:
            rechazados.append(f"{titulo[:40]} [cat={cat}]")

    print(f"\n  ⚽ Partidos soccer con outcomes válidos: {len(soccer_eventos)}")
    if not soccer_eventos and rechazados:
        print(f"  🔍 Rechazados (muestra): {rechazados[:5]}")

    # ── Agrupar por fecha y retornar la ventana más próxima ───────────────────
    por_fecha: dict[str, list] = {}
    for e in soccer_eventos:
        f = _fecha_evento(e)
        por_fecha.setdefault(f, []).append(e)

    fechas_disp = sorted(k for k in por_fecha if k != "sin-fecha")
    print(f"  📅 Fechas disponibles: {fechas_disp[:10]}")

    for fd in fechas_objetivo:
        if fd in por_fecha:
            partidos = por_fecha[fd]
            if fd != fechas_objetivo[0]:
                print(f"\n  ℹ️  Sin partidos hoy → mostrando {fd}")
            print(f"  ✅ {len(partidos)} partido(s) el {fd}:")
            for p in partidos[:10]:
                print(f"     ⚽ {p.get('title','?')[:65]}")
            return partidos

    if "sin-fecha" in por_fecha:
        return por_fecha["sin-fecha"]

    # Último recurso: devolver todos sin filtro de fecha
    if soccer_eventos:
        print(f"  ℹ️  Sin partidos en la ventana — retornando {len(soccer_eventos)} partidos encontrados")
        return soccer_eventos

    print(f"\n  ⚠️  0 partidos de soccer encontrados.")
    print(f"  → Polymarket puede no tener partidos de soccer activos en este momento.")
    return []


def es_mercado_1x2(pregunta: str, outcomes: list) -> bool:
    """
    v3.0: Detecta mercados Moneyline de fútbol con 2 O 3 outcomes.
    En Polymarket soccer la pregunta suele ser simplemente "Team A vs. Team B"
    y los outcomes incluyen Draw como outcome separado.
    """
    o_lower = [str(o).strip().lower() for o in outcomes]
    if all(o in {"yes", "no", "true", "false", "up", "down"} for o in o_lower):
        return False
    excluir = [
        "total goals", "both teams", "correct score", "first half", "second half",
        "anytime scorer", "yellow card", "red card", "corner", "penalty",
        "clean sheet", "player", "assists", "minutes", "hat trick",
        "own goal", "offside", "booking", "shot", "foul",
    ]
    if any(ex in pregunta.lower() for ex in excluir):
        return False
    # Acepta 2 o 3 outcomes que no sean sí/no
    return len(outcomes) in {2, 3}


def clasificar_mercado(pregunta: str, outcomes: list) -> str | None:
    """
    v3.0: Clasifica el tipo de mercado de fútbol.
    Soccer en Polymarket: la pregunta del moneyline ES el título del partido
    (ej: "Crystal Palace FC vs. Tottenham Hotspur FC").
    """
    pl = pregunta.lower()

    # Excluir mercados de props/estadísticas
    excluir = [
        "total goals", "both teams", "correct score", "first half", "second half",
        "anytime scorer", "yellow card", "red card", "corner", "penalty",
        "clean sheet", "player", "assists", "minutes", "hat trick",
        "who wins", "will there", "qualify", "relegated", "top scorer",
    ]
    if any(ex in pl for ex in excluir):
        return None

    if es_mercado_1x2(pregunta, outcomes):
        return "⚽ Moneyline 1X2"

    if "o/u" in pl or "over/under" in pl or "total" in pl:
        return "🎯 Total O/U"

    if "handicap" in pl or "spread" in pl:
        return "📐 Handicap"

    return None


def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:   return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except: return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:   return json.loads(raw) if isinstance(raw, str) else raw
    except: return []


def precio_clob(token_id: str) -> tuple[str, float | None]:
    try:
        r = SESSION.get(f"{CLOB_API}/midpoint",
                        params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        mid = r.json().get("mid")
        return token_id, float(mid) if mid is not None else None
    except Exception:
        return token_id, None


def obtener_precios_paralelo(token_ids: list[str]) -> dict[str, float]:
    resultado = {}
    with ThreadPoolExecutor(max_workers=30) as pool:
        futuros = {pool.submit(precio_clob, tid): tid for tid in token_ids}
        for f in as_completed(futuros):
            tid, precio = f.result()
            if precio is not None:
                resultado[tid] = precio
    return resultado


def construir_estructura(partidos: list[dict]) -> list[dict]:
    estructura = []
    no_1x2 = []

    for evento in partidos:
        candidatos = []
        for m in evento.get("markets", []):
            outcomes  = extraer_outcomes(m)
            tipo      = clasificar_mercado(m.get("question", ""), outcomes)
            if not tipo:
                continue
            token_ids = extraer_token_ids(m)
            if not token_ids:
                continue
            candidatos.append({
                "tipo":      tipo,
                "pregunta":  m.get("question", ""),
                "volumen":   float(m.get("volume", 0) or 0),
                "token_ids": token_ids,
                "outcomes":  outcomes,
            })

        seleccionados = {}
        for c in sorted(candidatos, key=lambda x: x["volumen"], reverse=True):
            if c["tipo"] not in seleccionados:
                seleccionados[c["tipo"]] = c
            if len(seleccionados) == 3:
                break

        if seleccionados and "⚽ Moneyline 1X2" in seleccionados:
            estructura.append({"evento": evento, "mercados": seleccionados})
        else:
            no_1x2.append(evento.get("title", "?")[:50])

    if no_1x2:
        print(f"\n  ℹ️  {len(no_1x2)} partido(s) descartados por no tener 1X2 válido:")
        for t in no_1x2[:5]:
            print(f"     - {t}")

    return estructura


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — GEMINI (análisis de fútbol con 3 outcomes)
# ══════════════════════════════════════════════════════════════════════════════

def _llamar_gemini_una_vez(client, equipo_local: str,
                            equipo_visitante: str) -> dict | None:
    """Una sola llamada a Gemini para un partido de fútbol."""
    prompt = f"""Eres un analista experto de apuestas deportivas de fútbol (soccer).
Necesito que analices el partido de HOY: {equipo_visitante} (visitante) @ {equipo_local} (local).

Usando búsqueda web, encuentra y responde EXACTAMENTE en este formato JSON (sin markdown, sin explicaciones):

{{
  "p_vegas_local": <número 0-100, probabilidad implícita del equipo LOCAL según casas de apuestas hoy>,
  "p_vegas_draw": <número 0-100, probabilidad implícita de EMPATE según casas de apuestas hoy>,
  "p_vegas_visitante": <número 0-100, probabilidad implícita del equipo VISITANTE según casas de apuestas hoy>,
  "n_local": <número -100 a 100, factor noticias equipo local: lesiones titulares (-), plantilla completa (+)>,
  "n_visitante": <número -100 a 100, factor noticias equipo visitante>,
  "r_local": <número 0-100, racha equipo local últimos 5 partidos: 5 victorias=100, 0 victorias=0>,
  "r_visitante": <número 0-100, racha equipo visitante últimos 5 partidos>,
  "titulares_bajos_local": <entero 0-5, número de titulares clave ausentes HOY en el equipo local>,
  "titulares_bajos_visitante": <entero 0-5, número de titulares clave ausentes HOY en el equipo visitante>,
  "liga": "<nombre de la liga o competición>",
  "resumen": "<2 oraciones: estado actual de ambos equipos, lesiones importantes y contexto del partido>"
}}

IMPORTANTE: p_vegas_local + p_vegas_draw + p_vegas_visitante deben sumar aproximadamente 100 (probabilidades implícitas con vig).

Busca específicamente:
1. Odds actuales en DraftKings, Bet365, William Hill o FanDuel para {equipo_local} vs {equipo_visitante}
2. Lesiones, suspensiones o ausencias confirmadas para HOY
3. Resultados de los últimos 5 partidos de cada equipo (W/D/L)
4. Cualquier contexto especial: rivalidad, motivación, cansancio por calendario

Responde SOLO el JSON."""

    try:
        respuesta_texto = ""
        for chunk in client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                tools=[types.Tool(googleSearch=types.GoogleSearch())],
            ),
        ):
            if chunk.text:
                respuesta_texto += chunk.text

        respuesta_texto = re.sub(r"```json|```", "", respuesta_texto).strip()
        match = re.search(r"\{.*\}", respuesta_texto, re.DOTALL)
        if match:
            data = json.loads(match.group())
            p_l = float(data.get("p_vegas_local", 40))
            p_d = float(data.get("p_vegas_draw", 25))
            p_v = float(data.get("p_vegas_visitante", 35))
            total = p_l + p_d + p_v
            if total > 0:
                p_l = p_l / total * 100
                p_d = p_d / total * 100
                p_v = p_v / total * 100
            return {
                "p_vegas_local":             p_l,
                "p_vegas_draw":              p_d,
                "p_vegas_visitante":         p_v,
                "n_local":                   float(data.get("n_local", 0)),
                "n_visitante":               float(data.get("n_visitante", 0)),
                "r_local":                   float(data.get("r_local", 50)),
                "r_visitante":               float(data.get("r_visitante", 50)),
                "titulares_bajos_local":     int(data.get("titulares_bajos_local", 0)),
                "titulares_bajos_visitante": int(data.get("titulares_bajos_visitante", 0)),
                "liga":                      data.get("liga", "Soccer"),
                "resumen":                   data.get("resumen", "Sin información disponible."),
            }
    except Exception as e:
        print(f"    ⚠️  Error Gemini (run): {e}")
    return None


def analizar_partido_con_gemini(equipo_local: str, equipo_visitante: str) -> dict:
    """Llama a Gemini GEMINI_RUNS veces y promedia."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("Variable de entorno GEMINI_API_KEY no configurada")
    client = genai.Client(api_key=api_key)

    resultados = []
    for i in range(GEMINI_RUNS):
        r = _llamar_gemini_una_vez(client, equipo_local, equipo_visitante)
        if r:
            resultados.append(r)

    if not resultados:
        return _valores_defecto()

    campos_num = [
        "p_vegas_local", "p_vegas_draw", "p_vegas_visitante",
        "n_local", "n_visitante", "r_local", "r_visitante",
        "titulares_bajos_local", "titulares_bajos_visitante",
    ]
    promedio = {c: sum(r[c] for r in resultados) / len(resultados) for c in campos_num}
    promedio["titulares_bajos_local"]     = round(promedio["titulares_bajos_local"])
    promedio["titulares_bajos_visitante"] = round(promedio["titulares_bajos_visitante"])
    promedio["resumen"] = resultados[-1]["resumen"]
    promedio["liga"]    = resultados[-1]["liga"]

    if len(resultados) > 1:
        for c in campos_num:
            vals = [f"{r[c]:.0f}" for r in resultados]
            prom = promedio[c]
            desv = max(abs(r[c] - prom) for r in resultados)
            flag = "  ⚠️ outlier" if desv > 20 else ""
            print(f"      {c:<28}: [{' | '.join(vals)}] → avg {prom:.1f}{flag}")

    return promedio


def _valores_defecto() -> dict:
    return {
        "p_vegas_local":             45.0,
        "p_vegas_draw":              25.0,
        "p_vegas_visitante":         30.0,
        "n_local":                   0.0,
        "n_visitante":               0.0,
        "r_local":                   50.0,
        "r_visitante":               50.0,
        "titulares_bajos_local":     0,
        "titulares_bajos_visitante": 0,
        "liga":                      "Soccer",
        "resumen":                   "Análisis no disponible.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — FÓRMULA SEA (Soccer Edge Alpha) con 3 outcomes
# ══════════════════════════════════════════════════════════════════════════════

def interpretar_sea(sea: float) -> tuple[str, str]:
    if sea <= -SCALP_UMBRAL:
        return "🎰 SCALPING", f"{abs(sea):.1f}pts descuento"
    if sea <= -SEA_UMBRAL:
        return "🔥 COMPRAR",  f"Precio {abs(sea):.1f}pts bajo valor real"
    if sea >= SEA_UMBRAL:
        return "❌ EVITAR",   f"Precio {sea:.1f}pts sobre valor real"
    return "➖ PRECIO JUSTO", f"SEA={sea:+.1f}"


def extraer_equipos(titulo: str) -> tuple[str, str]:
    """
    FIX v2: Extrae (equipo_visitante, equipo_local) del título del evento.
    Soporta: 'A vs B', 'A vs. B', 'A v B', 'Will A win vs B?'
    """
    # Limpiar prefijos comunes
    titulo_limpio = re.sub(r"^(will |who wins |match winner[:\s]*)", "", titulo, flags=re.IGNORECASE).strip()

    for sep in [" vs. ", " vs ", " v ", " @ "]:
        if sep in titulo_limpio:
            partes = titulo_limpio.split(sep, 1)
            # Limpiar sufijos como "- Match Winner", "?"
            local = re.sub(r"\s*[-–|].*$", "", partes[1]).strip().rstrip("?")
            visit = re.sub(r"\s*[-–|].*$", "", partes[0]).strip().rstrip("?")
            return visit, local

    return titulo, titulo


def detectar_rol_outcome(outcome: str, equipo_local: str,
                          equipo_visitante: str) -> str:
    """
    FIX v2: Determina si un outcome es 'local', 'visitante' o 'draw'.
    Más robusto: normaliza, busca substrings y maneja aliases comunes.
    """
    o  = outcome.lower().strip()
    el = equipo_local.lower().strip()
    ev = equipo_visitante.lower().strip()

    # Draw primero (más fácil de detectar)
    if o in DRAW_TERMS or "draw" in o or "tie" in o or "empate" in o:
        return "draw"

    # Coincidencia exacta o substring
    if el in o or o in el:
        return "local"
    if ev in o or o in ev:
        return "visitante"

    # FIX v2: Intentar con las primeras palabras del nombre del equipo
    el_parts = el.split()[:2]
    ev_parts = ev.split()[:2]
    if any(p in o for p in el_parts if len(p) > 3):
        return "local"
    if any(p in o for p in ev_parts if len(p) > 3):
        return "visitante"

    # FIX v2: Si los outcomes son ["Home", "Draw", "Away"] o similares
    if o in {"home", "1", "local"}:
        return "local"
    if o in {"away", "2", "visitante", "visitor"}:
        return "visitante"

    return "unknown"


def calcular_valor_raw_soccer(rol: str, analisis: dict):
    """
    Calcula el valor_raw para cada outcome (funciona con 2 O 3 outcomes).
    En mercados de 2 outcomes (sin Draw), el rol 'unknown' usa 50/50.
    """
    if rol == "local":
        p_vegas = analisis["p_vegas_local"]
        n       = analisis["n_local"]
        r       = analisis["r_local"]
        v       = +5.0
        tit     = analisis["titulares_bajos_local"]
    elif rol == "visitante":
        p_vegas = analisis["p_vegas_visitante"]
        n       = analisis["n_visitante"]
        r       = analisis["r_visitante"]
        v       = -5.0
        tit     = analisis["titulares_bajos_visitante"]
    elif rol == "draw":
        p_vegas = analisis["p_vegas_draw"]
        n       = (analisis["n_local"] + analisis["n_visitante"]) / 2
        r       = (analisis["r_local"] + analisis["r_visitante"]) / 2
        v       = 0.0
        tit     = 0
    else:
        # FIX v2.1: unknown → usar promedio de local/visitante como neutro
        p_vegas = (analisis["p_vegas_local"] + analisis["p_vegas_visitante"]) / 2
        n       = (analisis["n_local"] + analisis["n_visitante"]) / 2
        r       = (analisis["r_local"] + analisis["r_visitante"]) / 2
        v       = 0.0
        tit     = 0

    n_norm    = (n + 100) / 2
    valor_raw = 0.55 * p_vegas + 0.30 * n_norm + 0.10 * r + v

    if tit == 3:
        penalty = 0.10
    elif tit >= 4:
        penalty = 0.15
    else:
        penalty = 0.0

    valor_raw *= (1 - penalty)
    return valor_raw, v, n, n_norm, r, tit, penalty


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 4 — OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def hora_et(st: str) -> str:
    try:
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        return (dt - timedelta(hours=5)).strftime("%I:%M %p ET")
    except: return st


def barra(valor: float, total: float = 100, largo: int = 20) -> str:
    ratio = max(0, min(1, valor / total))
    lleno = int(ratio * largo)
    return "█" * lleno + "░" * (largo - lleno)


def imprimir_analisis(item: dict, analisis: dict,
                      precios: dict) -> tuple[list[dict], dict | None]:
    ev     = item["evento"]
    titulo = ev.get("title", "?")
    hora   = hora_et(ev.get("startTime") or ev.get("startDate") or "")
    vol    = float(ev.get("volume", 0) or 0)
    liga   = analisis.get("liga", "Soccer")

    equipo_visit, equipo_local = extraer_equipos(titulo)
    oportunidades = []
    quien_gana    = None

    print(f"\n{'═'*68}")
    print(f"  ⚽  {titulo.upper()}")
    print(f"  🏆  {liga}  |  ⏰ {hora}  |  Vol ${vol:,.0f}")
    print(f"{'═'*68}")
    print(f"  📰  {analisis['resumen']}")
    print(f"{'─'*68}")

    ml = item["mercados"].get("⚽ Moneyline 1X2")
    if not ml:
        print("  ⚠️  Sin mercado 1X2 disponible")
        return oportunidades, quien_gana

    # ── Pasada 1: calcular raw values ─────────────────────────────────────────
    outcomes_calc = []
    for outcome, token_id in zip(ml["outcomes"], ml["token_ids"]):
        precio_poly = precios.get(token_id)
        if precio_poly is None:
            print(f"  ⚠️  Sin precio CLOB para: {outcome}")
            continue

        rol    = detectar_rol_outcome(outcome, equipo_local, equipo_visit)
        result = calcular_valor_raw_soccer(rol, analisis)

        if isinstance(result, tuple):
            valor_raw, v_factor, n, n_norm, r, tit, penalty = result
        else:
            valor_raw = result
            v_factor  = 0.0
            n = n_norm = 50.0
            r = 50.0
            tit = penalty = 0

        outcomes_calc.append({
            "outcome":    outcome,
            "token_id":   token_id,
            "rol":        rol,
            "p_poly_pct": precio_poly * 100,
            "v_factor":   v_factor,
            "n":          n,
            "n_norm":     n_norm,
            "r":          r,
            "tit":        tit,
            "penalty":    penalty,
            "valor_raw":  valor_raw,
            "valor_real": valor_raw,
            "sea":        0.0,
            "hora":       hora,
            "partido":    titulo,
        })

    if not outcomes_calc:
        print("  ⚠️  Sin precios disponibles")
        return oportunidades, quien_gana

    # ── Normalizar valor_real a 100 ────────────────────────────────────────────
    total_vr = sum(oc["valor_raw"] for oc in outcomes_calc)
    if total_vr > 0:
        for oc in outcomes_calc:
            oc["valor_real"] = oc["valor_raw"] / total_vr * 100
            oc["sea"]        = oc["p_poly_pct"] - oc["valor_real"]

    # ── Pasada 2: imprimir ─────────────────────────────────────────────────────
    EMOJIS_ROL = {"local": "🏠", "visitante": "✈️ ", "draw": "🤝", "unknown": "❓"}

    for oc in outcomes_calc:
        emoji, desc = interpretar_sea(oc["sea"])
        if emoji == "🎰 SCALPING" and oc["valor_real"] < SCALP_REAL:
            emoji, desc = "🔥 COMPRAR", f"Precio {abs(oc['sea']):.1f}pts bajo valor real"

        icon  = EMOJIS_ROL.get(oc["rol"], "❓")
        label = oc["rol"].upper()

        if oc["rol"] == "local":
            p_vegas_label = analisis["p_vegas_local"]
        elif oc["rol"] == "visitante":
            p_vegas_label = analisis["p_vegas_visitante"]
        else:
            p_vegas_label = analisis["p_vegas_draw"]

        print(f"\n  {icon} {oc['outcome'].upper()} ({label})")
        print(f"     P_Poly  : {oc['p_poly_pct']:5.1f}  {barra(oc['p_poly_pct'])}")
        print(f"     P_Vegas : {p_vegas_label:5.1f}  {barra(p_vegas_label)}")
        print(f"     Noticias: {oc['n']:+5.1f}  (norm: {oc['n_norm']:.1f})")
        if oc["rol"] != "draw":
            print(f"     Localía : {oc['v_factor']:+5.1f}")
        print(f"     Racha   : {oc['r']:5.1f}  {barra(oc['r'])}")
        if oc["tit"] > 0:
            pen_str = (f"  ⚠️  penalización -{oc['penalty']*100:.0f}% aplicada"
                       if oc["penalty"] > 0 else "")
            print(f"     Titulares fuera: {oc['tit']}{pen_str}")
        print(f"     {'─'*50}")
        print(f"     Valor Real: {oc['valor_real']:.1f}¢")
        print(f"     SEA = {oc['p_poly_pct']:.1f} - {oc['valor_real']:.1f} = {oc['sea']:+.1f}")
        print(f"     {emoji}: {desc}")

        if abs(oc["sea"]) >= SEA_UMBRAL:
            if emoji == "🎰 SCALPING":
                accion = "SCALPING — comprar y vender pre-partido"
            elif oc["sea"] <= -SEA_UMBRAL:
                accion = "COMPRAR (precio bajo)"
            else:
                accion = "EVITAR (precio alto)"
            oportunidades.append({**oc, "accion": accion, "categoria": emoji})

    # ── QUIEN GANA ────────────────────────────────────────────────────────────
    if len(outcomes_calc) >= 2:
        max_oc = max(outcomes_calc, key=lambda x: x["valor_real"])
        rest   = [oc for oc in outcomes_calc if oc != max_oc]
        seg_oc = max(rest, key=lambda x: x["valor_real"]) if rest else max_oc
        gap    = max_oc["valor_real"] - seg_oc["valor_real"]

        if gap >= REAL_GAP_MIN:
            quien_gana = {
                "partido":       titulo,
                "hora":          hora,
                "liga":          liga,
                "favorito":      max_oc["outcome"],
                "favorito_rol":  max_oc["rol"],
                "favorito_real": max_oc["valor_real"],
                "favorito_poly": max_oc["p_poly_pct"],
                "favorito_sea":  max_oc["sea"],
                "segundo":       seg_oc["outcome"],
                "segundo_real":  seg_oc["valor_real"],
                "segundo_poly":  seg_oc["p_poly_pct"],
                "segundo_sea":   seg_oc["sea"],
                "gap":           gap,
                "outcomes_calc": outcomes_calc,
            }

    # ── Total O/U y Handicap ──────────────────────────────────────────────────
    tot = item["mercados"].get("🎯 Total O/U")
    hcp = item["mercados"].get("📐 Handicap")
    if tot or hcp:
        print(f"\n  {'─'*66}")
        print(f"  {'TOTAL O/U':<35} {'HANDICAP'}")
        n_rows = max(
            len(tot["outcomes"]) if tot else 0,
            len(hcp["outcomes"]) if hcp else 0,
        )
        for row in range(n_rows):
            tot_str = hcp_str = ""
            if tot and row < len(tot["outcomes"]):
                o, tid = tot["outcomes"][row], tot["token_ids"][row]
                p = precios.get(tid)
                if p:
                    try:
                        num    = tot["pregunta"].split("O/U")[1].strip() if "O/U" in tot["pregunta"] else ""
                        prefix = "O" if o.lower() == "over" else "U"
                        tot_str = f"  {prefix} {num}  →  {round(p*100)}¢"
                    except: tot_str = f"  {o}  →  {round(p*100)}¢"
            if hcp and row < len(hcp["outcomes"]):
                o, tid = hcp["outcomes"][row], hcp["token_ids"][row]
                p = precios.get(tid)
                if p:
                    hcp_str = f"  {o}  →  {round(p*100)}¢"
            print(f"  {tot_str:<35} {hcp_str}")

    return oportunidades, quien_gana


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 5 — GUARDAR RESULTADOS
# ══════════════════════════════════════════════════════════════════════════════

def guardar_resultados(todos_quienes: list[dict]) -> None:
    candidatos = []
    for qg in todos_quienes:
        candidatos.append({
            "equipo":  qg["favorito"],
            "partido": qg["partido"],
            "hora":    qg["hora"],
            "liga":    qg.get("liga", "Soccer"),
            "rol":     qg.get("favorito_rol", ""),
            "real":    round(qg["favorito_real"], 1),
            "poly":    round(qg["favorito_poly"], 1),
            "sea":     round(qg["favorito_sea"], 1),
            "gap":     round(qg["gap"], 1),
            "edge":    round(qg["favorito_real"] - qg["favorito_poly"], 1),
        })
    candidatos.sort(key=lambda x: x["gap"], reverse=True)

    data = {"fecha": str(date.today()), "candidatos": candidatos, "deporte": "soccer"}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultados_soccer.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 resultados_soccer.json guardado — {len(candidatos)} favorito(s)")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "╔" + "═"*66 + "╗")
    print("║" + "  ⚽  SOCCER EDGE ALPHA BOT  v3.0  —  Detector de Oportunidades".center(66) + "║")
    print("╚" + "═"*66 + "╝")
    print(f"\n  Fecha: {date.today()}")
    print(f"  Scalping  : SEA ≤ -{SCALP_UMBRAL} y valor_real ≥ {SCALP_REAL}¢")
    print(f"  Quien gana: gap real_values ≥ {REAL_GAP_MIN}¢ entre outcomes")
    print(f"  Ventana   : próximos {DIAS_VENTANA} días\n")
    print(f"  ℹ️  Mercados soportados: 1X2 (Local / Empate / Visitante)\n")

    # ── 1. Obtener partidos ───────────────────────────────────────────────────
    print("📡 [1/4] Cargando partidos desde Polymarket...")
    try:
        partidos = obtener_partidos_hoy()
    except Exception as e:
        print(f"  ❌ Error: {e}"); return

    if not partidos:
        print("  Sin partidos de soccer para hoy."); return
    print(f"  ✅ {len(partidos)} partido(s) encontrado(s)")

    estructura = construir_estructura(partidos)
    if not estructura:
        print("  ⚠️  No se encontraron partidos con mercados 1X2 válidos.")
        print("  Tip: Polymarket puede no tener partidos de soccer disponibles hoy.")
        return
    print(f"  📋 {len(estructura)} partido(s) con mercados 1X2 válidos")

    # ── 2. Precios CLOB ───────────────────────────────────────────────────────
    print("\n💹 [2/4] Obteniendo precios CLOB...")
    all_tokens = list({
        tid
        for item in estructura
        for m in item["mercados"].values()
        for tid in m["token_ids"]
    })
    precios = obtener_precios_paralelo(all_tokens)
    print(f"  ✅ {len(precios)}/{len(all_tokens)} precios obtenidos")

    # ── 3. Análisis Gemini ────────────────────────────────────────────────────
    print(f"\n🤖 [3/4] Analizando {len(estructura)} partido(s) con Gemini + Google Search...")
    analisis_por_partido = {}
    for item in estructura:
        titulo = item["evento"].get("title", "?")
        equipo_visit, equipo_local = extraer_equipos(titulo)
        print(f"  🔍 {titulo}  ({GEMINI_RUNS} runs → promedio)...")
        analisis = analizar_partido_con_gemini(equipo_local, equipo_visit)
        analisis_por_partido[titulo] = analisis
        print(f"     FINAL → Vegas L={analisis['p_vegas_local']:.1f}  "
              f"D={analisis['p_vegas_draw']:.1f}  "
              f"V={analisis['p_vegas_visitante']:.1f}  "
              f"N_l={analisis['n_local']:+.1f}  N_v={analisis['n_visitante']:+.1f}  "
              f"R_l={analisis['r_local']:.1f}  R_v={analisis['r_visitante']:.1f}")

    # ── 4. Calcular SEA ───────────────────────────────────────────────────────
    print(f"\n📊 [4/4] Calculando Soccer Edge Alpha (SEA)...\n")
    todas_ops     = []
    todos_quienes = []

    for item in estructura:
        titulo   = item["evento"].get("title", "?")
        analisis = analisis_por_partido.get(titulo, _valores_defecto())
        ops, qg  = imprimir_analisis(item, analisis, precios)
        todas_ops.extend(ops)
        if qg:
            todos_quienes.append(qg)

    # ══════════════════════════════════════════════════════════════════════════
    # RESUMEN FINAL
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'═'*68}")
    print(f"  📋  RESUMEN FINAL  —  SOCCER EDGE ALPHA v3.0")
    print(f"{'═'*68}")

    scalping = [o for o in todas_ops if o["categoria"] == "🎰 SCALPING"]
    print(f"\n  🎰  SCALPING  (SEA ≤ -{SCALP_UMBRAL} y real ≥ {SCALP_REAL}¢)")
    print(f"  {'─'*66}")
    if scalping:
        for op in sorted(scalping, key=lambda x: abs(x["sea"]), reverse=True):
            print(f"  ✔  {op['outcome']:<24} "
                  f"Poly {op['p_poly_pct']:5.1f}¢ → Real {op['valor_real']:5.1f}¢  "
                  f"SEA {op['sea']:+6.1f}  |  {op['hora']}")
            print(f"     {op['partido']}")
    else:
        print(f"  —  Ninguno hoy")

    print(f"\n  🏆  QUIEN GANA  (gap real_values ≥ {REAL_GAP_MIN}¢ entre outcomes)")
    print(f"  {'─'*66}")
    if todos_quienes:
        for qg in sorted(todos_quienes, key=lambda x: x["gap"], reverse=True):
            sea_fav = qg["favorito_sea"]
            if sea_fav <= 0:
                precio_label = "PRECIO BAJO ✅"
            elif sea_fav <= 10:
                precio_label = "precio ok"
            elif sea_fav <= 20:
                precio_label = "algo caro"
            else:
                precio_label = "CARO ⚠️"

            print(f"\n  ▶  {qg['partido']}  |  {qg['hora']}  |  {qg.get('liga', '')}")
            print(f"     Gap real: {qg['gap']:.1f}¢")
            print(f"     🏆 {qg['favorito']:<24} Real {qg['favorito_real']:5.1f}¢  "
                  f"Poly {qg['favorito_poly']:5.1f}¢  SEA {qg['favorito_sea']:+6.1f}  "
                  f"← {precio_label}")
            print(f"     👎 {qg['segundo']:<24} Real {qg['segundo_real']:5.1f}¢  "
                  f"Poly {qg['segundo_poly']:5.1f}¢  SEA {qg['segundo_sea']:+6.1f}")

            if "outcomes_calc" in qg and len(qg["outcomes_calc"]) == 3:
                tercer = [oc for oc in qg["outcomes_calc"]
                          if oc["outcome"] not in [qg["favorito"], qg["segundo"]]]
                if tercer:
                    t = tercer[0]
                    print(f"     🤝 {t['outcome']:<24} Real {t['valor_real']:5.1f}¢  "
                          f"Poly {t['p_poly_pct']:5.1f}¢  SEA {t['sea']:+6.1f}")
    else:
        print(f"  —  Ningún partido con diferencia ≥ {REAL_GAP_MIN}¢ hoy")

    print(f"\n{'═'*68}")
    print(f"  ⚠️  Solo informativo. No constituye consejo financiero.")
    print(f"{'═'*68}")

    guardar_resultados(todos_quienes)


if __name__ == "__main__":
    main()

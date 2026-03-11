"""
╔══════════════════════════════════════════════════════════════╗
║          SOCCER EDGE ALPHA BOT  v4.0                        ║
║  Detecta oportunidades de valor en Polymarket Soccer        ║
║                                                              ║
║  ARQUITECTURA:                                              ║
║  1. Gemini busca los partidos de fútbol de HOY en Polymarket║
║  2. Gemini analiza cada partido (odds Vegas, noticias, etc) ║
║  3. Polymarket: busca el evento por nombre y obtiene precios ║
║  4. Fórmula SEA calcula valor y oportunidades               ║
║                                                              ║
║  FÓRMULA SEA (Soccer Edge Alpha):                           ║
║  valor_raw  = 0.55·P_Vegas + 0.30·N_norm + 0.10·R + (±5V) ║
║  (Draw: sin ventaja de localía, promedio de ambos equipos)  ║
║  penalización titulares: -10% si >2 fuera, -15% si ≥4      ║
║  valor_real = normalizado a 100 entre los 3 outcomes        ║
║  SEA        = P_Poly - valor_real                           ║
║                                                              ║
║  🎰 SCALPING  : SEA ≤ -20 y valor_real ≥ 30               ║
║  🏆 QUIEN GANA: gap entre outcomes ≥ REAL_GAP_MIN          ║
║                                                              ║
║  Requiere: pip install requests google-genai                ║
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

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
SEA_UMBRAL   = 5.0
SCALP_UMBRAL = 20.0
SCALP_REAL   = 30.0
REAL_GAP_MIN = 12.0
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_RUNS  = 5

HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — GEMINI: obtener lista de partidos del día en Polymarket
# ══════════════════════════════════════════════════════════════════════════════

def obtener_partidos_gemini() -> list[dict]:
    """
    Usa Gemini + Google Search para obtener los partidos de fútbol
    disponibles HOY en Polymarket. Devuelve lista de dicts:
      { local, visitante, liga, hora_utc }
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("Variable de entorno GEMINI_API_KEY no configurada")
    client = genai.Client(api_key=api_key)

    hoy = date.today().strftime("%Y-%m-%d")
    prompt = f"""Busca en Polymarket (polymarket.com) los partidos de futbol (soccer) disponibles HOY {hoy}.

Busca en: https://polymarket.com/predictions/soccer

Devuelve SOLO un JSON con la lista de partidos encontrados en Polymarket hoy. Formato exacto:
{{
  "partidos": [
    {{
      "local": "<nombre exacto del equipo local tal como aparece en Polymarket>",
      "visitante": "<nombre exacto del equipo visitante tal como aparece en Polymarket>",
      "liga": "<nombre de la liga o competicion>",
      "hora_utc": "<hora aproximada en formato HH:MM UTC, o vacio si no disponible>"
    }}
  ]
}}

IMPORTANTE:
- Solo partidos de futbol (soccer), NO tenis, NBA, NFL u otros deportes
- Solo partidos con mercado activo en Polymarket HOY {hoy}
- Usa los nombres exactos de los equipos como aparecen en Polymarket
- Si no hay partidos hoy en Polymarket, devuelve {{"partidos": []}}
- Responde SOLO el JSON, sin markdown, sin explicaciones"""

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
            partidos = data.get("partidos", [])
            print(f"  Gemini encontro {len(partidos)} partido(s) en Polymarket")
            for p in partidos:
                print(f"     {p.get('local','?')} vs {p.get('visitante','?')}  [{p.get('liga','?')}]  {p.get('hora_utc','')}")
            return partidos
        else:
            print(f"  Gemini no devolvio JSON valido")
            print(f"  Respuesta: {respuesta_texto[:300]}")
            return []
    except Exception as e:
        print(f"  Error Gemini (lista partidos): {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — POLYMARKET: buscar evento y obtener precios
# ══════════════════════════════════════════════════════════════════════════════

def buscar_evento_polymarket(local: str, visitante: str) -> dict | None:
    """
    Busca en Polymarket el evento del partido.
    Pagina los eventos activos y busca por nombre de equipo.
    """
    local_l     = local.lower()
    visitante_l = visitante.lower()

    # Palabras clave del equipo local (ignorar palabras cortas o genéricas)
    STOP = {"fc", "cf", "sc", "afc", "the", "de", "del", "los", "las", "and"}
    palabras_local = [w for w in re.split(r'\W+', local_l)
                      if len(w) >= 4 and w not in STOP]
    palabras_visit = [w for w in re.split(r'\W+', visitante_l)
                      if len(w) >= 4 and w not in STOP]

    for offset in range(0, 2001, 200):
        try:
            r = SESSION.get(
                f"{GAMMA_API}/events",
                params={"order": "id", "ascending": "false",
                        "closed": "false", "active": "true",
                        "limit": 200, "offset": offset},
                timeout=20,
            )
            if r.status_code != 200:
                break
            data  = r.json()
            batch = data if isinstance(data, list) else data.get("data", [])
            if not batch:
                break

            for e in batch:
                titulo = e.get("title", "").lower()

                # Verificar que el evento tiene mercado con Draw
                tiene_draw = False
                for m in e.get("markets", []):
                    raw = m.get("outcomes", "[]")
                    try:
                        outs = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        outs = []
                    if any("draw" in str(o).lower() for o in outs):
                        tiene_draw = True
                        break
                if not tiene_draw:
                    continue

                # Match: al menos 1 palabra del local Y 1 del visitante en el título
                match_local = any(w in titulo for w in palabras_local) or local_l in titulo
                match_visit = any(w in titulo for w in palabras_visit) or visitante_l in titulo

                if match_local and match_visit:
                    print(f"     Encontrado: {e.get('title','?')[:60]}")
                    return e

            if len(batch) < 200:
                break

        except Exception as ex:
            print(f"     Error offset={offset}: {ex}")
            break

    print(f"     No encontrado en Polymarket: {local} vs {visitante}")
    return None


def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:   return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except: return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:   return json.loads(raw) if isinstance(raw, str) else raw
    except: return []


def seleccionar_mercado_1x2(evento: dict) -> dict | None:
    """Selecciona el mercado 1X2 con Draw de mayor volumen."""
    EXCLUIR = [
        "total goals", "both teams", "correct score", "first half", "second half",
        "anytime scorer", "yellow card", "red card", "corner", "penalty",
        "clean sheet", "player", "assists", "hat trick",
    ]
    candidatos = []
    for m in evento.get("markets", []):
        if any(ex in m.get("question", "").lower() for ex in EXCLUIR):
            continue
        outcomes  = extraer_outcomes(m)
        token_ids = extraer_token_ids(m)
        if not token_ids or len(token_ids) != len(outcomes):
            continue
        if not any("draw" in str(o).lower() for o in outcomes):
            continue
        candidatos.append({
            "pregunta":  m.get("question", ""),
            "volumen":   float(m.get("volume", 0) or 0),
            "token_ids": token_ids,
            "outcomes":  outcomes,
        })
    return max(candidatos, key=lambda x: x["volumen"]) if candidatos else None


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


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — GEMINI: análisis de partido (odds, noticias, rachas)
# ══════════════════════════════════════════════════════════════════════════════

def _llamar_gemini_una_vez(client, local: str, visitante: str) -> dict | None:
    prompt = f"""Eres un analista experto de apuestas deportivas de futbol (soccer).
Analiza el partido de HOY: {visitante} (visitante) @ {local} (local).

Usando busqueda web, responde EXACTAMENTE en este formato JSON (sin markdown):

{{
  "p_vegas_local": <0-100, probabilidad implicita del LOCAL segun casas de apuestas hoy>,
  "p_vegas_draw": <0-100, probabilidad implicita de EMPATE segun casas de apuestas hoy>,
  "p_vegas_visitante": <0-100, probabilidad implicita del VISITANTE segun casas de apuestas hoy>,
  "n_local": <-100 a 100, factor noticias local: lesiones (-), plantilla completa (+)>,
  "n_visitante": <-100 a 100, factor noticias visitante>,
  "r_local": <0-100, racha local ultimos 5 partidos: 5W=100, 0W=0>,
  "r_visitante": <0-100, racha visitante ultimos 5 partidos>,
  "titulares_bajos_local": <0-5, titulares clave ausentes HOY en local>,
  "titulares_bajos_visitante": <0-5, titulares clave ausentes HOY en visitante>,
  "liga": "<nombre de la liga>",
  "resumen": "<2 oraciones: estado ambos equipos, lesiones y contexto>"
}}

IMPORTANTE: p_vegas_local + p_vegas_draw + p_vegas_visitante deben sumar ~100.
Busca: odds Bet365/DraftKings/William Hill, lesiones confirmadas, ultimos 5 resultados.
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
                p_l, p_d, p_v = p_l/total*100, p_d/total*100, p_v/total*100
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
                "liga":    data.get("liga", "Soccer"),
                "resumen": data.get("resumen", "Sin informacion disponible."),
            }
    except Exception as e:
        print(f"    Error Gemini run: {e}")
    return None


def analizar_partido(local: str, visitante: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY no configurada")
    client = genai.Client(api_key=api_key)

    resultados = []
    for _ in range(GEMINI_RUNS):
        r = _llamar_gemini_una_vez(client, local, visitante)
        if r:
            resultados.append(r)

    if not resultados:
        return _valores_defecto()

    campos_num = ["p_vegas_local", "p_vegas_draw", "p_vegas_visitante",
                  "n_local", "n_visitante", "r_local", "r_visitante",
                  "titulares_bajos_local", "titulares_bajos_visitante"]
    prom = {c: sum(r[c] for r in resultados) / len(resultados) for c in campos_num}
    prom["titulares_bajos_local"]     = round(prom["titulares_bajos_local"])
    prom["titulares_bajos_visitante"] = round(prom["titulares_bajos_visitante"])
    prom["resumen"] = resultados[-1]["resumen"]
    prom["liga"]    = resultados[-1]["liga"]

    if len(resultados) > 1:
        for c in campos_num:
            vals = [f"{r[c]:.0f}" for r in resultados]
            avg  = prom[c]
            desv = max(abs(r[c] - avg) for r in resultados)
            flag = "  OUTLIER" if desv > 20 else ""
            print(f"      {c:<28}: [{' | '.join(vals)}] -> avg {avg:.1f}{flag}")

    return prom


def _valores_defecto() -> dict:
    return {
        "p_vegas_local": 45.0, "p_vegas_draw": 25.0, "p_vegas_visitante": 30.0,
        "n_local": 0.0, "n_visitante": 0.0,
        "r_local": 50.0, "r_visitante": 50.0,
        "titulares_bajos_local": 0, "titulares_bajos_visitante": 0,
        "liga": "Soccer", "resumen": "Analisis no disponible.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 4 — FÓRMULA SEA + OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def interpretar_sea(sea: float) -> tuple[str, str]:
    if sea <= -SCALP_UMBRAL:
        return "SCALPING", f"{abs(sea):.1f}pts descuento"
    if sea <= -SEA_UMBRAL:
        return "COMPRAR",  f"Precio {abs(sea):.1f}pts bajo valor real"
    if sea >= SEA_UMBRAL:
        return "EVITAR",   f"Precio {sea:.1f}pts sobre valor real"
    return "PRECIO JUSTO", f"SEA={sea:+.1f}"


def barra(valor: float, total: float = 100, largo: int = 20) -> str:
    ratio = max(0, min(1, valor / total))
    lleno = int(ratio * largo)
    return "█" * lleno + "░" * (largo - lleno)


def calcular_y_mostrar(partido_info: dict, analisis: dict,
                        mercado: dict, precios: dict) -> tuple[list[dict], dict | None]:
    local     = partido_info["local"]
    visitante = partido_info["visitante"]
    liga      = analisis.get("liga", partido_info.get("liga", "Soccer"))
    hora      = partido_info.get("hora_utc", "")
    titulo    = f"{local} vs. {visitante}"

    print(f"\n{'='*68}")
    print(f"  {titulo.upper()}")
    print(f"  {liga}   |   {hora}")
    print(f"{'='*68}")
    print(f"  {analisis['resumen']}")
    print(f"{'-'*68}")

    oportunidades = []
    quien_gana    = None

    outcomes  = mercado["outcomes"]
    token_ids = mercado["token_ids"]

    def detectar_rol(outcome_str: str) -> str:
        o = outcome_str.lower().strip()
        if "draw" in o:
            return "draw"
        if local.lower() in o or o in local.lower():
            return "local"
        if visitante.lower() in o or o in visitante.lower():
            return "visitante"
        idx = outcomes.index(outcome_str)
        return ["local", "draw", "visitante"][min(idx, 2)]

    outcomes_calc = []
    for outcome, token_id in zip(outcomes, token_ids):
        precio_poly = precios.get(token_id)
        if precio_poly is None:
            print(f"  Sin precio CLOB para: {outcome}")
            continue

        p_poly_pct = precio_poly * 100
        rol        = detectar_rol(outcome)

        if rol == "local":
            p_vegas         = analisis["p_vegas_local"]
            n               = analisis["n_local"]
            r               = analisis["r_local"]
            titulares_bajos = analisis["titulares_bajos_local"]
            v_factor        = +5.0
        elif rol == "visitante":
            p_vegas         = analisis["p_vegas_visitante"]
            n               = analisis["n_visitante"]
            r               = analisis["r_visitante"]
            titulares_bajos = analisis["titulares_bajos_visitante"]
            v_factor        = -5.0
        else:
            p_vegas         = analisis["p_vegas_draw"]
            n               = (analisis["n_local"] + analisis["n_visitante"]) / 2
            r               = (analisis["r_local"] + analisis["r_visitante"]) / 2
            titulares_bajos = 0
            v_factor        = 0.0

        n_norm    = (n + 100) / 2
        valor_raw = 0.55 * p_vegas + 0.30 * n_norm + 0.10 * r + v_factor

        if titulares_bajos == 3:
            penalty = 0.10
        elif titulares_bajos >= 4:
            penalty = 0.15
        else:
            penalty = 0.0
        valor_raw *= (1 - penalty)

        outcomes_calc.append({
            "outcome":        outcome,
            "token_id":       token_id,
            "rol":            rol,
            "p_poly_pct":     p_poly_pct,
            "p_vegas":        p_vegas,
            "n":              n,
            "n_norm":         n_norm,
            "v_factor":       v_factor,
            "r":              r,
            "titulares_bajos": titulares_bajos,
            "penalty":        penalty,
            "valor_raw":      valor_raw,
            "valor_real":     valor_raw,
            "sea":            0.0,
            "partido":        titulo,
            "hora":           hora,
            "liga":           liga,
        })

    if not outcomes_calc:
        print("  Sin precios disponibles")
        return oportunidades, quien_gana

    total_vr = sum(oc["valor_raw"] for oc in outcomes_calc)
    if total_vr > 0:
        for oc in outcomes_calc:
            oc["valor_real"] = oc["valor_raw"] / total_vr * 100
            oc["sea"]        = oc["p_poly_pct"] - oc["valor_real"]

    ROL_LABEL = {"local": "LOCAL    ", "visitante": "VISITANTE", "draw": "EMPATE   "}
    ROL_ICON  = {"local": "[Casa]", "visitante": "[Visit]", "draw": "[Draw]"}

    for oc in outcomes_calc:
        etiqueta, desc = interpretar_sea(oc["sea"])
        if etiqueta == "SCALPING" and oc["valor_real"] < SCALP_REAL:
            etiqueta, desc = "COMPRAR", f"Precio {abs(oc['sea']):.1f}pts bajo valor real"

        label = ROL_LABEL.get(oc["rol"], oc["rol"].upper())
        icon  = ROL_ICON.get(oc["rol"], "")
        print(f"\n  {icon} {oc['outcome'][:30]:<30} ({label})")
        print(f"     P_Poly  : {oc['p_poly_pct']:5.1f}  {barra(oc['p_poly_pct'])}")
        print(f"     P_Vegas : {oc['p_vegas']:5.1f}  {barra(oc['p_vegas'])}")
        print(f"     Noticias: {oc['n']:+5.1f}  (norm: {oc['n_norm']:.1f})")
        if oc["rol"] != "draw":
            print(f"     Localia : {oc['v_factor']:+5.1f}")
        print(f"     Racha   : {oc['r']:5.1f}  {barra(oc['r'])}")
        if oc["titulares_bajos"] > 0:
            pen_str = f"  -{oc['penalty']*100:.0f}% aplicado" if oc["penalty"] > 0 else ""
            print(f"     Titulares ausentes: {oc['titulares_bajos']}{pen_str}")
        print(f"     {'-'*50}")
        print(f"     Valor Real: {oc['valor_real']:.1f}c")
        print(f"     SEA = {oc['p_poly_pct']:.1f} - {oc['valor_real']:.1f} = {oc['sea']:+.1f}")
        print(f"     >> {etiqueta}: {desc}")

        if abs(oc["sea"]) >= SEA_UMBRAL:
            accion = ("SCALPING" if etiqueta == "SCALPING" else
                      "COMPRAR" if oc["sea"] <= -SEA_UMBRAL else "EVITAR")
            oportunidades.append({**oc, "accion": accion, "categoria": etiqueta})

    no_draw = [oc for oc in outcomes_calc if oc["rol"] != "draw"]
    if len(no_draw) >= 2:
        sorted_nd = sorted(no_draw, key=lambda x: x["valor_real"], reverse=True)
        mejor  = sorted_nd[0]
        segundo = sorted_nd[1]
        gap = mejor["valor_real"] - segundo["valor_real"]
        if gap >= REAL_GAP_MIN:
            quien_gana = {
                "partido":       titulo,
                "hora":          hora,
                "liga":          liga,
                "favorito":      mejor["outcome"],
                "favorito_rol":  mejor["rol"],
                "favorito_real": mejor["valor_real"],
                "favorito_poly": mejor["p_poly_pct"],
                "favorito_sea":  mejor["sea"],
                "segundo":       segundo["outcome"],
                "segundo_real":  segundo["valor_real"],
                "segundo_poly":  segundo["p_poly_pct"],
                "segundo_sea":   segundo["sea"],
                "gap":           gap,
                "outcomes_calc": outcomes_calc,
            }

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
            "sea":     round(qg["favorito_sea"],  1),
            "gap":     round(qg["gap"],            1),
            "edge":    round(qg["favorito_real"] - qg["favorito_poly"], 1),
        })
    candidatos.sort(key=lambda x: x["gap"], reverse=True)

    data = {"fecha": str(date.today()), "candidatos": candidatos, "deporte": "soccer"}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultados_soccer.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  resultados_soccer.json guardado -- {len(candidatos)} favorito(s)")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 68)
    print("  SOCCER EDGE ALPHA BOT  v4.0  --  Detector de Oportunidades")
    print("=" * 68)
    print(f"\n  Fecha   : {date.today()}")
    print(f"  Scalping: SEA <= -{SCALP_UMBRAL} y valor_real >= {SCALP_REAL}c")
    print(f"  Quien gana: gap >= {REAL_GAP_MIN}c entre outcomes\n")

    # 1. Gemini busca los partidos del dia en Polymarket
    print("[1/4] Buscando partidos de hoy en Polymarket via Gemini...")
    try:
        partidos_hoy = obtener_partidos_gemini()
    except Exception as e:
        print(f"  ERROR: {e}"); return

    if not partidos_hoy:
        print("  Sin partidos de soccer en Polymarket hoy.")
        return
    print(f"  {len(partidos_hoy)} partido(s) a procesar\n")

    # 2. Para cada partido: buscar en Polymarket y obtener precios CLOB
    print("[2/4] Buscando eventos en Polymarket y obteniendo precios CLOB...")
    partidos_con_mercado = []

    for p in partidos_hoy:
        local     = p.get("local", "")
        visitante = p.get("visitante", "")
        print(f"\n  >> {local} vs {visitante}")

        evento = buscar_evento_polymarket(local, visitante)
        if not evento:
            continue

        mercado = seleccionar_mercado_1x2(evento)
        if not mercado:
            print(f"     Sin mercado 1X2 con Draw")
            continue

        token_ids = mercado["token_ids"]
        precios   = obtener_precios_paralelo(token_ids)
        print(f"     {len(precios)}/{len(token_ids)} precios CLOB obtenidos")
        print(f"     Outcomes: {mercado['outcomes']}")

        partidos_con_mercado.append({
            "info":    p,
            "evento":  evento,
            "mercado": mercado,
            "precios": precios,
        })

    if not partidos_con_mercado:
        print("\n  Sin partidos con mercado 1X2 valido en Polymarket.")
        return
    print(f"\n  {len(partidos_con_mercado)} partido(s) con mercado y precios\n")

    # 3. Analisis Gemini por partido
    print(f"[3/4] Analizando partidos con Gemini + Google Search...")
    for item in partidos_con_mercado:
        p = item["info"]
        print(f"\n  >> {p['local']} vs {p['visitante']}  ({GEMINI_RUNS} runs)...")
        analisis = analizar_partido(p["local"], p["visitante"])
        item["analisis"] = analisis
        print(f"     Vegas L={analisis['p_vegas_local']:.1f}  "
              f"D={analisis['p_vegas_draw']:.1f}  V={analisis['p_vegas_visitante']:.1f}  "
              f"N_l={analisis['n_local']:+.1f}  N_v={analisis['n_visitante']:+.1f}  "
              f"R_l={analisis['r_local']:.1f}  R_v={analisis['r_visitante']:.1f}")

    # 4. Calcular SEA
    print(f"\n[4/4] Calculando Soccer Edge Alpha (SEA)...\n")
    todas_ops     = []
    todos_quienes = []

    for item in partidos_con_mercado:
        ops, qg = calcular_y_mostrar(
            item["info"], item["analisis"],
            item["mercado"], item["precios"],
        )
        todas_ops.extend(ops)
        if qg:
            todos_quienes.append(qg)

    # RESUMEN FINAL
    print(f"\n\n{'='*68}")
    print(f"  RESUMEN FINAL  --  SOCCER EDGE ALPHA v4.0")
    print(f"{'='*68}")

    scalping = [o for o in todas_ops if o["categoria"] == "SCALPING"]
    print(f"\n  SCALPING  (SEA <= -{SCALP_UMBRAL} y real >= {SCALP_REAL}c)")
    print(f"  {'-'*66}")
    if scalping:
        for op in sorted(scalping, key=lambda x: abs(x["sea"]), reverse=True):
            print(f"  >> {op['outcome']:<26} "
                  f"Poly {op['p_poly_pct']:5.1f}c -> Real {op['valor_real']:5.1f}c  "
                  f"SEA {op['sea']:+6.1f}  |  {op['hora']}")
            print(f"     {op['partido']}")
    else:
        print(f"  -- Ninguno hoy")

    print(f"\n  QUIEN GANA  (gap real_values >= {REAL_GAP_MIN}c entre equipos)")
    print(f"  {'-'*66}")
    if todos_quienes:
        for qg in sorted(todos_quienes, key=lambda x: x["gap"], reverse=True):
            sea_fav = qg["favorito_sea"]
            precio_label = (
                "PRECIO BAJO" if sea_fav <= 0 else
                "precio ok"  if sea_fav <= 10 else
                "algo caro"  if sea_fav <= 20 else
                "CARO"
            )
            print(f"\n  >> {qg['partido']}  |  {qg['hora']}  |  {qg.get('liga','')}")
            print(f"     Gap: {qg['gap']:.1f}c")
            print(f"     [1] {qg['favorito']:<26} Real {qg['favorito_real']:5.1f}c  "
                  f"Poly {qg['favorito_poly']:5.1f}c  SEA {qg['favorito_sea']:+6.1f}  "
                  f"<- {precio_label}")
            print(f"     [2] {qg['segundo']:<26} Real {qg['segundo_real']:5.1f}c  "
                  f"Poly {qg['segundo_poly']:5.1f}c  SEA {qg['segundo_sea']:+6.1f}")
            draw_oc = next((oc for oc in qg.get("outcomes_calc", [])
                            if oc["rol"] == "draw"), None)
            if draw_oc:
                print(f"     [X] {draw_oc['outcome']:<26} Real {draw_oc['valor_real']:5.1f}c  "
                      f"Poly {draw_oc['p_poly_pct']:5.1f}c  SEA {draw_oc['sea']:+6.1f}")
    else:
        print(f"  -- Ningun partido con diferencia >= {REAL_GAP_MIN}c hoy")

    print(f"\n{'='*68}")
    print(f"  Solo informativo. No constituye consejo financiero.")
    print(f"{'='*68}")

    guardar_resultados(todos_quienes)


if __name__ == "__main__":
    main()

"""
╔══════════════════════════════════════════════════════════════╗
║          NBA EDGE ALPHA BOT  v3.5                           ║
║  Detecta oportunidades de valor en Polymarket NBA           ║
║                                                              ║
║  FÓRMULA NEA (NBA Edge Alpha):                              ║
║  valor_raw  = 0.55·P_Vegas + 0.30·N_norm + 0.10·R + (±5V) ║
║  penalización estrellas: -10% si >2 fuera, -15% si ≥4     ║
║  valor_real = normalizado a 100 entre ambos equipos         ║
║  NEA        = P_Poly - valor_real                           ║
║                                                              ║
║  RESUMEN FINAL:                                             ║
║  🎰 SCALPING  : NEA ≤ -20 y valor_real ≥ 40               ║
║                 Comprar pre-partido, vender antes tip-off   ║
║  🏆 QUIEN GANA: equipo con mayor real_value cuando el gap  ║
║                 entre los dos equipos es ≥ REAL_GAP_MIN    ║
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

GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
NBA_SERIES_ID  = 10345
NEA_UMBRAL     = 5.0
SCALP_UMBRAL   = 20.0   # NEA mínimo (absoluto) para calificar como scalping
SCALP_REAL     = 40.0   # valor_real mínimo para scalping
REAL_GAP_MIN   = 15.0   # diferencia mínima entre real_values para "quien gana"
GEMINI_MODEL   = "gemini-flash-lite-latest"

HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — POLYMARKET (Gamma + CLOB)
# ══════════════════════════════════════════════════════════════════════════════

def obtener_partidos_hoy() -> list[dict]:
    hoy = date.today().strftime("%Y-%m-%d")
    resp = SESSION.get(
        f"{GAMMA_API}/events",
        params={
            "series_id": NBA_SERIES_ID, "tag_id": 100639,
            "active": "true", "closed": "false",
            "limit": 100, "order": "startTime", "ascending": "true",
        }, timeout=15
    )
    resp.raise_for_status()
    todos = resp.json()
    return [e for e in todos if hoy in str(e.get("eventDate", ""))]


def clasificar_mercado(pregunta: str) -> str | None:
    p, pl = pregunta.strip(), pregunta.lower()
    excluir = [
        "points o/u", "rebounds o/u", "assists o/u", "steals o/u",
        "blocks o/u", "turnovers o/u", "3-pointer", "field goal", "free throw",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "first half", "second half", "halftime", "triple double", "double double",
        "will there be", "lead at any", "margin of victory", "largest lead",
    ]
    if any(ex in pl for ex in excluir): return None
    if p.startswith("Spread:"):                          return "📐 Spread"
    if ": O/U" in p:                                     return "🎯 Total O/U"
    if ("vs." in pl or " vs " in pl) and ":" not in p:  return "💰 Moneyline"
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
    for evento in partidos:
        candidatos = []
        for m in evento.get("markets", []):
            tipo = clasificar_mercado(m.get("question", ""))
            if not tipo: continue
            token_ids = extraer_token_ids(m)
            if not token_ids: continue
            candidatos.append({
                "tipo":      tipo,
                "pregunta":  m.get("question", ""),
                "volumen":   float(m.get("volume", 0) or 0),
                "token_ids": token_ids,
                "outcomes":  extraer_outcomes(m),
            })
        seleccionados = {}
        for c in sorted(candidatos, key=lambda x: x["volumen"], reverse=True):
            if c["tipo"] not in seleccionados:
                seleccionados[c["tipo"]] = c
            if len(seleccionados) == 3: break
        if seleccionados:
            estructura.append({"evento": evento, "mercados": seleccionados})
    return estructura


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — GEMINI
# ══════════════════════════════════════════════════════════════════════════════

GEMINI_RUNS = 5   # cuántas veces consultar Gemini por partido y promediar


def _llamar_gemini_una_vez(client, equipo_local: str,
                            equipo_visitante: str) -> dict | None:
    """Una sola llamada a Gemini. Devuelve dict con los valores o None si falla."""
    prompt = f"""Eres un analista experto de apuestas deportivas NBA.
Necesito que analices el partido de HOY: {equipo_visitante} (visitante) @ {equipo_local} (local).

Usando búsqueda web, encuentra y responde EXACTAMENTE en este formato JSON (sin markdown, sin explicaciones):

{{
  "p_vegas": <número 0-100, probabilidad implícita del equipo LOCAL según las casas de apuestas hoy>,
  "n_local": <número -100 a 100, factor noticias equipo local: lesiones clave (-), alineación completa (+)>,
  "n_visitante": <número -100 a 100, factor noticias equipo visitante>,
  "r_local": <número 0-100, racha equipo local últimos 5 partidos: 5 victorias=100, 0 victorias=0>,
  "r_visitante": <número 0-100, racha equipo visitante últimos 5 partidos>,
  "estrellas_bajas_local": <entero 0-5, número de jugadores All-Star o >18 PPG ausentes HOY en el equipo local>,
  "estrellas_bajas_visitante": <entero 0-5, número de jugadores All-Star o >18 PPG ausentes HOY en el equipo visitante>,
  "resumen": "<2 oraciones: estado actual de ambos equipos, lesiones importantes y contexto del partido>"
}}

Busca específicamente:
1. Odds actuales de casas como DraftKings, FanDuel o BetMGM para {equipo_local} vs {equipo_visitante}
2. Lesiones o ausencias confirmadas para HOY — en especial jugadores All-Star o con >18 PPG de promedio
3. Resultados de los últimos 5 partidos de cada equipo

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
            return {
                "p_vegas":                  float(data.get("p_vegas", 50)),
                "n_local":                  float(data.get("n_local", 0)),
                "n_visitante":              float(data.get("n_visitante", 0)),
                "r_local":                  float(data.get("r_local", 50)),
                "r_visitante":              float(data.get("r_visitante", 50)),
                "estrellas_bajas_local":    int(data.get("estrellas_bajas_local", 0)),
                "estrellas_bajas_visitante": int(data.get("estrellas_bajas_visitante", 0)),
                "resumen":                  data.get("resumen", "Sin información disponible."),
            }
    except Exception as e:
        print(f"    ⚠️  Error Gemini (run): {e}")
    return None


def analizar_partido_con_gemini(equipo_local: str, equipo_visitante: str,
                                 linea_ml_local: float) -> dict:
    """
    Llama a Gemini GEMINI_RUNS veces y promedia los valores numéricos.
    Esto reduce outliers causados por respuestas inconsistentes de la API.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("Variable de entorno GEMINI_API_KEY no configurada")
    client  = genai.Client(api_key=api_key)

    resultados = []
    for i in range(GEMINI_RUNS):
        r = _llamar_gemini_una_vez(client, equipo_local, equipo_visitante)
        if r:
            resultados.append(r)

    if not resultados:
        return _valores_defecto(linea_ml_local)

    # Promediar todos los valores numéricos entre los runs
    campos = ["p_vegas", "n_local", "n_visitante", "r_local", "r_visitante",
              "estrellas_bajas_local", "estrellas_bajas_visitante"]
    promedio = {c: sum(r[c] for r in resultados) / len(resultados) for c in campos}
    # Redondear conteos de estrellas al entero más cercano
    promedio["estrellas_bajas_local"]     = round(promedio["estrellas_bajas_local"])
    promedio["estrellas_bajas_visitante"] = round(promedio["estrellas_bajas_visitante"])
    promedio["resumen"] = resultados[-1]["resumen"]   # resumen del último run

    # Mostrar valores individuales si hubo más de un run (para detectar outliers)
    if len(resultados) > 1:
        for c in campos:
            vals = [f"{r[c]:.0f}" for r in resultados]
            prom = promedio[c]
            desv = max(abs(r[c] - prom) for r in resultados)
            flag = "  ⚠️ outlier" if desv > 20 else ""
            print(f"      {c:<14}: [{' | '.join(vals)}] → avg {prom:.1f}{flag}")

    return promedio


def _valores_defecto(linea_ml_local: float) -> dict:
    return {
        "p_vegas":                  linea_ml_local * 100,
        "n_local":                  0.0,
        "n_visitante":              0.0,
        "r_local":                  50.0,
        "r_visitante":              50.0,
        "estrellas_bajas_local":    0,
        "estrellas_bajas_visitante": 0,
        "resumen":                  "Análisis no disponible.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — FÓRMULA NEA
# ══════════════════════════════════════════════════════════════════════════════

def interpretar_nea(nea: float) -> tuple[str, str]:
    if nea <= -SCALP_UMBRAL:
        return "🎰 SCALPING", f"{abs(nea):.1f}pts descuento"
    if nea <= -NEA_UMBRAL:
        return "🔥 COMPRAR",  f"Precio {abs(nea):.1f}pts bajo valor real"
    if nea >= NEA_UMBRAL:
        return "❌ EVITAR",   f"Precio {nea:.1f}pts sobre valor real"
    return "➖ PRECIO JUSTO", f"NEA={nea:+.1f}"


def extraer_equipos(titulo: str) -> tuple[str, str]:
    for sep in [" vs. ", " vs "]:
        if sep in titulo:
            partes = titulo.split(sep, 1)
            return partes[0].strip(), partes[1].strip()
    return titulo, titulo


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
    """
    Devuelve:
      - lista de oportunidades individuales (scalping / comprar / evitar)
      - dict con el pronóstico 'quien gana' si el gap entre real values ≥ REAL_GAP_MIN
    """
    ev     = item["evento"]
    titulo = ev.get("title", "?")
    hora   = hora_et(ev.get("startTime", ""))
    vol    = float(ev.get("volume", 0) or 0)

    equipo_visit, equipo_local = extraer_equipos(titulo)
    oportunidades = []
    quien_gana    = None

    print(f"\n{'═'*68}")
    print(f"  🏀  {titulo.upper()}")
    print(f"  ⏰  {hora}   |   Vol ${vol:,.0f}")
    print(f"{'═'*68}")
    print(f"  📰  {analisis['resumen']}")
    print(f"{'─'*68}")

    ml = item["mercados"].get("💰 Moneyline")
    if not ml:
        print("  ⚠️  Sin mercado Moneyline disponible")
        return oportunidades, quien_gana

    # ── Pasada 1: calcular valores para todos los equipos ─────────────────────
    equipos_calc = []
    for outcome, token_id in zip(ml["outcomes"], ml["token_ids"]):
        precio_poly = precios.get(token_id)
        if precio_poly is None:
            print(f"  ⚠️  Sin precio CLOB para: {outcome}")
            continue

        p_poly_pct = precio_poly * 100
        es_local   = (outcome.lower() == equipo_local.lower())

        v_factor = 5.0 if es_local else -5.0

        if es_local:
            p_vegas          = analisis["p_vegas"]
            n                = analisis["n_local"]
            r                = analisis["r_local"]
            estrellas_bajas  = analisis["estrellas_bajas_local"]
        else:
            p_vegas          = 100 - analisis["p_vegas"]
            n                = analisis["n_visitante"]
            r                = analisis["r_visitante"]
            estrellas_bajas  = analisis["estrellas_bajas_visitante"]

        n_norm    = (n + 100) / 2
        # Fix 2+3: pesos redistribuidos; V_factor (±5) se aplica como aditivo directo
        valor_raw = 0.55 * p_vegas + 0.30 * n_norm + 0.10 * r + v_factor

        # Penalización por estrellas ausentes (independiente de racha y localía)
        # >2 estrellas (All-Star / >18 PPG) fuera → -10%; ≥4 fuera → -15%
        if estrellas_bajas == 3:
            penalty_pct = 0.10
        elif estrellas_bajas >= 4:
            penalty_pct = 0.15
        else:
            penalty_pct = 0.0
        valor_raw *= (1 - penalty_pct)

        nea = p_poly_pct - valor_raw   # provisional, se recalcula tras normalizar

        equipos_calc.append({
            "outcome":         outcome,
            "token_id":        token_id,
            "es_local":        es_local,
            "p_poly_pct":      p_poly_pct,
            "p_vegas":         p_vegas,
            "n":               n,
            "n_norm":          n_norm,
            "v_factor":        v_factor,
            "r":               r,
            "estrellas_bajas": estrellas_bajas,
            "penalty_pct":     penalty_pct,
            "valor_raw":       valor_raw,
            "valor_real":      valor_raw,   # se normalizará a continuación
            "nea":             nea,
            "hora":            hora,
            "partido":         titulo,
        })

    if not equipos_calc:
        print("  ⚠️  Sin precios disponibles")
        return oportunidades, quien_gana

    # ── Fix 1: Normalizar valor_real a 100 (mercado binario) ──────────────────
    if len(equipos_calc) == 2:
        total_vr = sum(ec["valor_raw"] for ec in equipos_calc)
        if total_vr > 0:
            for ec in equipos_calc:
                ec["valor_real"] = ec["valor_raw"] / total_vr * 100
                ec["nea"] = ec["p_poly_pct"] - ec["valor_real"]

    # ── Pasada 2: imprimir cada equipo ────────────────────────────────────────
    for ec in equipos_calc:
        emoji, desc = interpretar_nea(ec["nea"])
        # SCALPING requiere también real ≥ SCALP_REAL
        if emoji == "🎰 SCALPING" and ec["valor_real"] < SCALP_REAL:
            emoji, desc = "🔥 COMPRAR", f"Precio {abs(ec['nea']):.1f}pts bajo valor real"

        rol  = "LOCAL   " if ec["es_local"] else "VISITANTE"
        icon = "🏠" if ec["es_local"] else "✈️ "
        print(f"\n  {icon} {ec['outcome'].upper()} ({rol})")
        print(f"     P_Poly  : {ec['p_poly_pct']:5.1f}  {barra(ec['p_poly_pct'])}")
        print(f"     P_Vegas : {ec['p_vegas']:5.1f}  {barra(ec['p_vegas'])}")
        print(f"     Noticias: {ec['n']:+5.1f}  (norm: {ec['n_norm']:.1f})")
        print(f"     Localía : {ec['v_factor']:+5.1f}")
        print(f"     Racha   : {ec['r']:5.1f}  {barra(ec['r'])}")
        if ec["estrellas_bajas"] > 0:
            penalty_str = (f"  ⚠️  penalización -{ec['penalty_pct']*100:.0f}% aplicada"
                           if ec["penalty_pct"] > 0 else "")
            print(f"     Estrellas fuera: {ec['estrellas_bajas']}{penalty_str}")
        print(f"     {'─'*50}")
        print(f"     Valor Real: {ec['valor_real']:.1f}¢")
        print(f"     NEA = {ec['p_poly_pct']:.1f} - {ec['valor_real']:.1f} = {ec['nea']:+.1f}")
        print(f"     {emoji}: {desc}")

        if abs(ec["nea"]) >= NEA_UMBRAL:
            accion = ("SCALPING — comprar y vender pre-partido"
                      if emoji == "🎰 SCALPING" else
                      "COMPRAR (precio bajo)"
                      if ec["nea"] <= -NEA_UMBRAL else
                      "EVITAR (precio alto)")
            oportunidades.append({**ec, "accion": accion, "categoria": emoji})

    # ── Calcular QUIEN GANA para este partido ────────────────────────────────
    if len(equipos_calc) == 2:
        a, b  = equipos_calc[0], equipos_calc[1]
        gap   = abs(a["valor_real"] - b["valor_real"])
        if gap >= REAL_GAP_MIN:
            favorito  = a if a["valor_real"] > b["valor_real"] else b
            underdog  = b if a["valor_real"] > b["valor_real"] else a
            quien_gana = {
                "partido":          titulo,
                "hora":             hora,
                "favorito":         favorito["outcome"],
                "favorito_real":    favorito["valor_real"],
                "favorito_poly":    favorito["p_poly_pct"],
                "favorito_nea":     favorito["nea"],
                "underdog":         underdog["outcome"],
                "underdog_real":    underdog["valor_real"],
                "underdog_poly":    underdog["p_poly_pct"],
                "underdog_nea":     underdog["nea"],
                "gap":              gap,
            }

    # ── Spread y Total como referencia ────────────────────────────────────────
    spr = item["mercados"].get("📐 Spread")
    tot = item["mercados"].get("🎯 Total O/U")
    print(f"\n  {'─'*66}")
    print(f"  {'SPREAD':<32} {'TOTAL'}")
    n_rows = max(
        len(spr["outcomes"]) if spr else 0,
        len(tot["outcomes"]) if tot else 0,
    )
    for row in range(n_rows):
        spr_str = tot_str = ""
        if spr and row < len(spr["outcomes"]):
            o, tid = spr["outcomes"][row], spr["token_ids"][row]
            p = precios.get(tid)
            if p:
                try:
                    pts   = spr["pregunta"].split("(")[1].rstrip(")")
                    pts_f = float(pts)
                    fav   = spr["pregunta"].split(":")[1].split("(")[0].strip()
                    pts_l = f"{pts_f:+.1f}" if o == fav else f"{-pts_f:+.1f}"
                except: pts_l = ""
                spr_str = f"  {o} {pts_l}  →  {round(p*100)}¢"
        if tot and row < len(tot["outcomes"]):
            o, tid = tot["outcomes"][row], tot["token_ids"][row]
            p = precios.get(tid)
            if p:
                try:
                    num    = tot["pregunta"].split("O/U")[1].strip()
                    prefix = "O" if o.lower() == "over" else "U"
                    tot_str = f"  {prefix} {num}  →  {round(p*100)}¢"
                except: tot_str = f"  {o}  →  {round(p*100)}¢"
        print(f"  {spr_str:<32} {tot_str}")

    return oportunidades, quien_gana


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 5 — GUARDAR RESULTADOS PARA LA CALCULADORA WEB
# ══════════════════════════════════════════════════════════════════════════════

def guardar_resultados(todos_quienes: list[dict]) -> None:
    """
    Serializa los favoritos de 'quien gana' en resultados.json.
    La calculadora web (app.py / index.html) lee este archivo.
    """
    candidatos = []
    for qg in todos_quienes:
        candidatos.append({
            "equipo":  qg["favorito"],
            "partido": qg["partido"],
            "hora":    qg["hora"],
            "real":    round(qg["favorito_real"], 1),
            "poly":    round(qg["favorito_poly"], 1),
            "nea":     round(qg["favorito_nea"],  1),
            "gap":     round(qg["gap"],            1),
            "edge":    round(qg["favorito_real"] - qg["favorito_poly"], 1),
        })
    candidatos.sort(key=lambda x: x["gap"], reverse=True)

    data = {"fecha": str(date.today()), "candidatos": candidatos}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultados.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 resultados.json guardado — {len(candidatos)} favorito(s) para la calculadora")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "╔" + "═"*66 + "╗")
    print("║" + "  🏀  NBA EDGE ALPHA BOT  v3.5  —  Detector de Oportunidades".center(66) + "║")
    print("╚" + "═"*66 + "╝")
    print(f"\n  Fecha: {date.today()}")
    print(f"  Scalping : NEA ≤ -{SCALP_UMBRAL} y valor_real ≥ {SCALP_REAL}¢")
    print(f"  Quien gana: gap real_values ≥ {REAL_GAP_MIN}¢ entre los dos equipos\n")

    # ── 1. Obtener partidos ───────────────────────────────────────────────────
    print("📡 [1/4] Cargando partidos desde Polymarket...")
    try:
        partidos = obtener_partidos_hoy()
    except Exception as e:
        print(f"  ❌ Error: {e}"); return

    if not partidos:
        print("  Sin partidos para hoy."); return
    print(f"  ✅ {len(partidos)} partido(s) encontrado(s)")

    estructura = construir_estructura(partidos)
    print(f"  📋 {len(estructura)} partido(s) con mercados válidos")

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
        ml = item["mercados"].get("💰 Moneyline")
        p_local_clob = 0.5
        if ml:
            for outcome, tid in zip(ml["outcomes"], ml["token_ids"]):
                if outcome.lower() == equipo_local.lower() and tid in precios:
                    p_local_clob = precios[tid]; break
        print(f"  🔍 {titulo}  ({GEMINI_RUNS} runs → promedio)...")
        analisis = analizar_partido_con_gemini(equipo_local, equipo_visit, p_local_clob)
        analisis_por_partido[titulo] = analisis
        print(f"     FINAL → Vegas={analisis['p_vegas']:.1f}  "
              f"N_local={analisis['n_local']:+.1f}  "
              f"N_visit={analisis['n_visitante']:+.1f}  "
              f"R_local={analisis['r_local']:.1f}  "
              f"R_visit={analisis['r_visitante']:.1f}  "
              f"Stars_local={analisis['estrellas_bajas_local']}  "
              f"Stars_visit={analisis['estrellas_bajas_visitante']}")

    # ── 4. Calcular NEA y mostrar análisis ────────────────────────────────────
    print(f"\n📊 [4/4] Calculando NBA Edge Alpha (NEA)...\n")
    todas_ops    = []
    todos_quienes = []

    for item in estructura:
        titulo   = item["evento"].get("title", "?")
        analisis = analisis_por_partido.get(titulo, _valores_defecto(0.5))
        ops, qg  = imprimir_analisis(item, analisis, precios)
        todas_ops.extend(ops)
        if qg:
            todos_quienes.append(qg)

    # ══════════════════════════════════════════════════════════════════════════
    # RESUMEN FINAL
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'═'*68}")
    print(f"  📋  RESUMEN FINAL")
    print(f"{'═'*68}")

    # ── SCALPING ──────────────────────────────────────────────────────────────
    scalping = [o for o in todas_ops if o["categoria"] == "🎰 SCALPING"]
    print(f"\n  🎰  SCALPING  (NEA ≤ -{SCALP_UMBRAL} y real ≥ {SCALP_REAL}¢)")
    print(f"  {'─'*66}")
    if scalping:
        for op in sorted(scalping, key=lambda x: abs(x["nea"]), reverse=True):
            print(f"  ✔  {op['outcome']:<22} "
                  f"Poly {op['p_poly_pct']:5.1f}¢ → Real {op['valor_real']:5.1f}¢  "
                  f"NEA {op['nea']:+6.1f}  |  {op['hora']}")
            print(f"     {op['partido']}")
    else:
        print(f"  —  Ninguno hoy")

    # ── QUIEN GANA ────────────────────────────────────────────────────────────
    print(f"\n  🏆  QUIEN GANA  (gap real_values ≥ {REAL_GAP_MIN}¢ entre equipos)")
    print(f"  {'─'*66}")
    if todos_quienes:
        for qg in sorted(todos_quienes, key=lambda x: x["gap"], reverse=True):
            # Evaluar si el precio del favorito es aceptable
            nea_fav = qg["favorito_nea"]
            if nea_fav <= 0:
                precio_label = "PRECIO BAJO ✅"
            elif nea_fav <= 10:
                precio_label = "precio ok"
            elif nea_fav <= 20:
                precio_label = "algo caro"
            else:
                precio_label = "CARO ⚠️"

            print(f"\n  ▶  {qg['partido']}  |  {qg['hora']}")
            print(f"     Gap real: {qg['gap']:.1f}¢")
            print(f"     🏆 {qg['favorito']:<20} Real {qg['favorito_real']:5.1f}¢  "
                  f"Poly {qg['favorito_poly']:5.1f}¢  NEA {qg['favorito_nea']:+6.1f}  "
                  f"← {precio_label}")
            print(f"     👎 {qg['underdog']:<20} Real {qg['underdog_real']:5.1f}¢  "
                  f"Poly {qg['underdog_poly']:5.1f}¢  NEA {qg['underdog_nea']:+6.1f}")
    else:
        print(f"  —  Ningún partido con diferencia ≥ {REAL_GAP_MIN}¢ hoy")

    print(f"\n{'═'*68}")
    print(f"  ⚠️  Solo informativo. No constituye consejo financiero.")
    print(f"{'═'*68}")

    # ── Guardar resultados para la calculadora web ───────────────────────────
    guardar_resultados(todos_quienes)


if __name__ == "__main__":
    main()

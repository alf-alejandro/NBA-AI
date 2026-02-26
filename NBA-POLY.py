"""
Polymarket — Partidos NBA del día
- Gamma API: partidos y mercados del día
- CLOB API:  precios reales en paralelo (ThreadPoolExecutor)
"""

import requests
import json
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
NBA_SERIES_ID = 10345
HEADERS       = {"User-Agent": "Mozilla/5.0"}
SESSION       = requests.Session()
SESSION.headers.update(HEADERS)


# ── Gamma: partidos del día ───────────────────────────────────────────────────

def obtener_partidos_hoy() -> list[dict]:
    hoy = date.today().strftime("%Y-%m-%d")
    print(f"📅 Fecha: {hoy}")
    print("🔍 Gamma API: buscando partidos NBA...\n")

    resp = SESSION.get(
        f"{GAMMA_API}/events",
        params={
            "series_id": NBA_SERIES_ID, "tag_id": 100639,
            "active": "true", "closed": "false",
            "limit": 50, "order": "startTime", "ascending": "true",
        }, timeout=15
    )
    resp.raise_for_status()
    todos = resp.json()
    partidos = [e for e in todos if e.get("eventDate") == hoy]

    if not partidos:
        fechas = sorted(set(e.get("eventDate", "?") for e in todos))
        print(f"⚠️  Sin partidos para {hoy}. Próximas fechas: {fechas}")
    return partidos


# ── Clasificación exacta según patrones de la API ────────────────────────────

def clasificar_mercado(pregunta: str) -> str | None:
    p  = pregunta.strip()
    pl = p.lower()

    excluir = [
        "points o/u", "rebounds o/u", "assists o/u", "steals o/u",
        "blocks o/u", "turnovers o/u", "3-pointer", "field goal", "free throw",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "first half", "second half", "halftime",
        "triple double", "double double", "will there be",
        "lead at any", "margin of victory", "largest lead",
    ]
    if any(ex in pl for ex in excluir):
        return None

    if p.startswith("Spread:"):     return "📐 Spread"
    if ": O/U" in p:                return "🎯 Total O/U"
    if "vs." in pl and ":" not in p: return "💰 Moneyline"
    return None


# ── CLOB: precio individual ───────────────────────────────────────────────────

def precio_clob(token_id: str) -> tuple[str, float | None]:
    """Devuelve (token_id, midpoint) — se llama en paralelo."""
    try:
        r = SESSION.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=8
        )
        r.raise_for_status()
        mid = r.json().get("mid")
        return token_id, float(mid) if mid is not None else None
    except Exception:
        return token_id, None


def obtener_precios_paralelo(token_ids: list[str]) -> dict[str, float]:
    """Consulta todos los tokens en paralelo (máx 20 workers)."""
    resultado = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        futuros = {pool.submit(precio_clob, tid): tid for tid in token_ids}
        for futuro in as_completed(futuros):
            tid, precio = futuro.result()
            if precio is not None:
                resultado[tid] = precio
    return resultado


# ── Helpers ───────────────────────────────────────────────────────────────────

def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:
        return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except Exception:
        return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []


def hora_et(st: str) -> str:
    try:
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        return (dt - timedelta(hours=5)).strftime("%I:%M %p ET")
    except Exception:
        return st


def centavos(precio: float) -> str:
    return f"{round(precio * 100)}¢"


def parse_spread_label(pregunta: str, outcome: str) -> str:
    """Extrae el número del spread para el outcome correcto.
    'Spread: Pistons (-5.5)' con outcome='Pistons' → '-5.5'
    'Spread: Pistons (-5.5)' con outcome='Thunder'  → '+5.5'
    """
    try:
        # equipo favorito y puntos están en la pregunta
        inside = pregunta.split("(")[1].rstrip(")")   # ej: "-5.5"
        pts    = float(inside)
        fav    = pregunta.split(":")[1].split("(")[0].strip()  # ej: "Pistons"
        if outcome == fav:
            return f"{pts:+.1f}".replace(".0", "")
        else:
            return f"{-pts:+.1f}".replace(".0", "")
    except Exception:
        return ""


def parse_total_linea(pregunta: str, outcome: str) -> str:
    """'Thunder vs. Pistons: O/U 218.5', outcome='Over' → 'O 218.5'"""
    try:
        num = pregunta.split("O/U")[1].strip()
        prefix = "O" if outcome.lower() == "over" else "U"
        return f"{prefix} {num}"
    except Exception:
        return outcome


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*64)
    print("  🏀  POLYMARKET — NBA HOY  (precios CLOB en tiempo real)")
    print("="*64 + "\n")

    partidos = obtener_partidos_hoy()
    if not partidos:
        return
    print(f"  ✅ {len(partidos)} partido(s) encontrado(s)\n")

    # ── Seleccionar los 3 mercados principales por partido ────────────────────
    ORDEN = {"💰 Moneyline": 0, "📐 Spread": 1, "🎯 Total O/U": 2}
    estructura = []

    for evento in partidos:
        candidatos = []
        for m in evento.get("markets", []):
            tipo = clasificar_mercado(m.get("question", ""))
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
                "outcomes":  extraer_outcomes(m),
            })

        # Mayor volumen por tipo
        seleccionados = {}
        for c in sorted(candidatos, key=lambda x: x["volumen"], reverse=True):
            if c["tipo"] not in seleccionados:
                seleccionados[c["tipo"]] = c
            if len(seleccionados) == 3:
                break

        estructura.append({"evento": evento, "mercados": seleccionados})

    # ── Recolectar todos los token_ids únicos ─────────────────────────────────
    all_tokens = list({
        tid
        for item in estructura
        for m in item["mercados"].values()
        for tid in m["token_ids"]
    })

    print(f"💹 CLOB API: consultando {len(all_tokens)} tokens en paralelo...\n")
    precios = obtener_precios_paralelo(all_tokens)
    print(f"   ✅ {len(precios)}/{len(all_tokens)} precios obtenidos\n")

    # ── Mostrar ───────────────────────────────────────────────────────────────
    for item in estructura:
        ev     = item["evento"]
        titulo = ev.get("title", "?")
        hora   = hora_et(ev.get("startTime", ""))
        vol    = float(ev.get("volume", 0) or 0)
        liq    = float(ev.get("liquidity", 0) or 0)

        print(f"{'─'*64}")
        print(f"  🏀  {titulo}")
        print(f"       ⏰ {hora}   |   Vol ${vol:,.0f}   |   Liq ${liq:,.0f}")
        print()

        ml_m  = item["mercados"].get("💰 Moneyline")
        spr_m = item["mercados"].get("📐 Spread")
        tot_m = item["mercados"].get("🎯 Total O/U")

        # Construir filas (1 por outcome, normalmente 2)
        n_rows = max(
            len(ml_m["outcomes"])  if ml_m  else 0,
            len(spr_m["outcomes"]) if spr_m else 0,
            len(tot_m["outcomes"]) if tot_m else 0,
        )

        # Encabezado
        print(f"       {'MONEYLINE':<22} {'SPREAD':<22} {'TOTAL'}")

        for row in range(n_rows):
            ml_str = spr_str = tot_str = ""

            if ml_m and row < len(ml_m["outcomes"]):
                outcome = ml_m["outcomes"][row]
                tid     = ml_m["token_ids"][row]
                precio  = precios.get(tid)
                if precio is not None:
                    ml_str = f"{outcome} {centavos(precio)}"

            if spr_m and row < len(spr_m["outcomes"]):
                outcome = spr_m["outcomes"][row]
                tid     = spr_m["token_ids"][row]
                precio  = precios.get(tid)
                if precio is not None:
                    pts = parse_spread_label(spr_m["pregunta"], outcome)
                    spr_str = f"{outcome} {pts} {centavos(precio)}"

            if tot_m and row < len(tot_m["outcomes"]):
                outcome = tot_m["outcomes"][row]
                tid     = tot_m["token_ids"][row]
                precio  = precios.get(tid)
                if precio is not None:
                    lbl = parse_total_linea(tot_m["pregunta"], outcome)
                    tot_str = f"{lbl} {centavos(precio)}"

            print(f"       {ml_str:<22} {spr_str:<22} {tot_str}")

        print()

    print("="*64 + "\n")


if __name__ == "__main__":
    main()
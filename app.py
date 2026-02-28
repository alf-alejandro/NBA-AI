"""
NBA Edge Alpha Bot — Web Dashboard
Ejecuta el análisis en background y hace streaming del output al browser via SSE.
"""

import os
import sys
import json
import time
import threading
import importlib.util
from flask import Flask, render_template, Response, jsonify

app = Flask(__name__)

# ── Cargar NBA-AI como módulo (el nombre tiene guión, no se puede importar directo)
_spec = importlib.util.spec_from_file_location(
    "nba_ai", os.path.join(os.path.dirname(__file__), "NBA-AI.py")
)
nba_ai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nba_ai)

# ── Estado global del análisis ────────────────────────────────────────────────
_state: dict = {
    "running":   False,
    "completed": False,
    "output":    [],
    "error":     None,
}
_lock = threading.Lock()


class _Capture:
    """Redirige stdout al buffer compartido y también lo muestra en terminal."""

    def __init__(self, original):
        self.original = original

    def write(self, text: str):
        self.original.write(text)
        stripped = text.rstrip("\n")
        if stripped:
            with _lock:
                _state["output"].append(stripped)

    def flush(self):
        self.original.flush()

    def isatty(self):
        return False


def _run_analysis():
    old_out = sys.stdout
    sys.stdout = _Capture(old_out)
    try:
        nba_ai.main()
        with _lock:
            _state["completed"] = True
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
            _state["output"].append(f"❌ ERROR: {exc}")
    finally:
        sys.stdout = old_out
        with _lock:
            _state["running"] = False


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    with _lock:
        if _state["running"]:
            return jsonify({"error": "Ya hay un análisis en curso"}), 409
        _state["running"]   = True
        _state["completed"] = False
        _state["output"]    = []
        _state["error"]     = None

    threading.Thread(target=_run_analysis, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/stream")
def stream():
    """Server-Sent Events: envía cada línea nueva al cliente en tiempo real."""
    def generate():
        sent = 0
        while True:
            with _lock:
                new_lines = _state["output"][sent:]
                running   = _state["running"]
                completed = _state["completed"]

            for line in new_lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1

            if not running:
                yield f"data: {json.dumps({'done': True, 'completed': completed})}\n\n"
                return

            time.sleep(0.15)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/resultados")
def resultados():
    path = os.path.join(os.path.dirname(__file__), "resultados.json")
    if not os.path.exists(path):
        return jsonify({"error": "Sin resultados. Ejecuta el análisis primero."}), 404
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "running":   _state["running"],
            "completed": _state["completed"],
            "lines":     len(_state["output"]),
            "error":     _state["error"],
        })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

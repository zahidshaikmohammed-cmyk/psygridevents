"""Read-only JSON API + minimal dashboard (stdlib ThreadingHTTPServer, no extra dependency).

The engine runs on the asyncio thread and *publishes* an immutable snapshot
after every cycle; HTTP threads only read that snapshot or query SQLite, so
an API request can never block or corrupt signal generation.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .alerts import format_signal
from .storage import Store

log = logging.getLogger("psygridevents.api")


class Published:
    """Thread-safe holder of the latest engine publication (replaced atomically)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"generated_at": None, "top": [], "signals": [], "providers": [],
                                      "market": {}, "system": {}, "metrics": {}}

    def set(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._data


def _int(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(query.get(name, [default])[0])))
    except (TypeError, ValueError):
        return default


def _float(query: dict[str, list[str]], name: str, default: float) -> float:
    try:
        return float(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


DASHBOARD = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>PSYGRID Events</title>
<style>
:root{--bg:#0f1115;--fg:#e8e8e8;--mut:#9aa0a6;--card:#181b22;--long:#2fbf71;--short:#ef5350;--line:#2a2e37}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--fg:#15171a;--mut:#5f6368;--card:#fff;--line:#dde1e6}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}.sym{font-weight:700;font-size:17px}
.LONG{color:var(--long)}.SHORT{color:var(--short)}.pill{border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
.wrap{overflow-x:auto}
</style></head><body><main>
<h1>PSYGRID event signals</h1><div class=mut id=meta>loading…</div>
<div id=top></div><h2 style="font-size:15px">Providers</h2><div class="card wrap"><table id=prov></table></div>
<p class=mut>Opportunity score is a transparent heuristic, not a probability. Not investment advice.</p>
</main><script>
const pct=v=>v==null?'n/a':(v*100).toFixed(2)+'%';
async function load(){
 try{
  const [t,p,s]=await Promise.all([fetch('signals/top').then(r=>r.json()),fetch('providers').then(r=>r.json()),fetch('system/status').then(r=>r.json())]);
  document.getElementById('meta').textContent=`phase ${s.phase} · market ${s.market_health} · updated ${t.generated_at||'-'}`;
  const top=document.getElementById('top');top.innerHTML='';
  if(!t.signals.length){top.innerHTML='<div class=card>No signal currently clears the quality threshold.</div>'}
  for(const x of t.signals){const d=document.createElement('div');d.className='card';
   d.innerHTML=`<div class=row><span class=pill>#${x.rank}</span><span class=sym>${x.symbol}</span><span class="${x.direction}">${x.direction}</span><span class=pill>${x.signal_state}</span><span>score ${x.opportunity_score}</span><span class=mut>${(x.freshness||{}).event_age||''}</span></div>
   <div>${(x.event||{}).headline||''}</div><div class=mut>move ${pct((x.market_reaction||{}).move_since_event)} · RS ${(x.relative_strength||{}).label} · VWAP ${x.VWAP_state} · exhaustion ${x.exhaustion_state}</div>
   <div>${x.why_now||''}</div><div class=mut>Invalidation: ${x.invalidation||''}</div>`;top.appendChild(d)}
  const tb=document.getElementById('prov');tb.innerHTML='<tr><th>Provider</th><th>Quality</th><th>State</th><th>Last success</th><th>Error</th></tr>';
  for(const x of p.providers){const r=document.createElement('tr');r.innerHTML=`<td>${x.name}</td><td>${x.quality}</td><td>${x.state}</td><td>${x.last_success_at||'-'}</td><td class=mut>${(x.last_error||'').slice(0,80)}</td>`;tb.appendChild(r)}
 }catch(e){document.getElementById('meta').textContent='API unreachable: '+e}
}
load();setInterval(load,15000);
</script></body></html>"""


class ApiServer:
    def __init__(self, host: str, port: int, published: Published, store: Store,
                 liveness: Callable[[], dict[str, Any]]) -> None:
        self.published = published
        self.store = store
        self.liveness = liveness
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="api", daemon=True)

    def start(self) -> None:
        self.thread.start()
        log.info("API listening on http://%s:%s", *self.httpd.server_address[:2])

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ routes
    def route(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any, str]:
        data = self.published.get()
        if path in ("/", "/dashboard"):
            return 200, DASHBOARD, "text/html; charset=utf-8"
        if path == "/health":
            live = self.liveness()
            return (200 if live["status"] in ("OK", "DEGRADED", "STARTING") else 503), live, "application/json"
        if path == "/providers":
            return 200, {"generated_at": data.get("generated_at"), "providers": data.get("providers", [])}, "application/json"
        if path == "/events":
            since = datetime.now(timezone.utc) - timedelta(hours=_float(query, "since_hours", 24.0))
            symbol = (query.get("symbol") or [None])[0]
            events = self.store.events(since=since, symbol=symbol, limit=_int(query, "limit", 200, 2000))
            return 200, {"count": len(events), "events": events}, "application/json"
        if path == "/events/latest":
            events = self.store.events(limit=_int(query, "limit", 50, 500))
            return 200, {"count": len(events), "events": events}, "application/json"
        if path == "/stories":
            since = datetime.now(timezone.utc) - timedelta(hours=_float(query, "since_hours", 24.0))
            stories = self.store.stories(since=since, limit=_int(query, "limit", 200, 2000))
            for story in stories:
                story["evidence"] = self.store.observations_for_story(story["story_id"])[:20]
                for item in story["evidence"]:
                    item.pop("raw", None)
            return 200, {"count": len(stories), "stories": stories}, "application/json"
        if path == "/signals":
            state = (query.get("state") or [None])[0]
            signals = [item for item in data.get("signals", []) if not state or item.get("signal_state") == state.upper()]
            return 200, {"generated_at": data.get("generated_at"), "count": len(signals), "signals": signals}, "application/json"
        if path == "/signals/top":
            top = data.get("top", [])
            body: dict[str, Any] = {
                "generated_at": data.get("generated_at"), "phase": data.get("system", {}).get("phase"),
                "min_opportunity_score": data.get("system", {}).get("min_opportunity_score"),
                "score_is_probability": False, "count": len(top), "signals": top,
            }
            if (query.get("format") or [""])[0] == "text":
                text = "\n\n".join(format_signal(item) for item in top) or "No signal currently clears the quality threshold."
                return 200, text, "text/plain; charset=utf-8"
            return 200, body, "application/json"
        if path.startswith("/signals/"):
            symbol = unquote(path.split("/", 2)[2]).upper()
            active = [item for item in data.get("signals", []) if item.get("symbol") == symbol]
            history = [
                {"signal_key": item.signal_key, "state": item.state, "opportunity_score": item.opportunity_score,
                 "trade_date": item.trade_date, "event_id": item.event_id, "updated": item.last_state_change_at.isoformat()}
                for item in self.store.signals_for_symbol(symbol)
            ]
            events = self.store.events(symbol=symbol, limit=20)
            return 200, {"symbol": symbol, "active": active, "history": history, "events": events}, "application/json"
        if path == "/market/health":
            return 200, data.get("market", {}), "application/json"
        if path == "/system/status":
            return 200, data.get("system", {}) | {"liveness": self.liveness(), "database": self.store.counts()}, "application/json"
        if path == "/metrics":
            metrics = data.get("metrics", {})
            if (query.get("format") or [""])[0] == "prometheus":
                lines = []
                for key, value in sorted(metrics.items()):
                    if isinstance(value, (int, float)):
                        lines.append(f"psygridevents_{key} {value}")
                return 200, "\n".join(lines) + "\n", "text/plain; version=0.0.4"
            return 200, metrics, "application/json"
        if path == "/outcomes":
            items = self.store.outcomes(limit=_int(query, "limit", 200, 5000))
            return 200, {"count": len(items), "outcomes": items}, "application/json"
        if path == "/outcomes/stats":
            return 200, self.store.outcome_statistics(), "application/json"
        if path == "/diagnostics/daily":
            return 200, {"days": self.store.daily_diagnostics(_int(query, "limit", 10, 120))}, "application/json"
        if path == "/transitions":
            since = datetime.now(timezone.utc) - timedelta(hours=_float(query, "since_hours", 12.0))
            return 200, {"transitions": self.store.transitions(since=since, limit=_int(query, "limit", 500, 5000))}, "application/json"
        return 404, {"error": "not found", "path": path}, "application/json"

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "psygridevents"

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                path = parts.path.rstrip("/") or "/"
                try:
                    status, body, content_type = server.route(path, parse_qs(parts.query))
                except Exception as exc:  # noqa: BLE001
                    log.exception("API error on %s", path)
                    status, body, content_type = 500, {"error": type(exc).__name__}, "application/json"
                payload = body if isinstance(body, str) else json.dumps(body, default=str, ensure_ascii=False)
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("api %s", fmt % args)

        return Handler

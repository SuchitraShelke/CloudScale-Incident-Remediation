"""Generates observability/grafana/dashboards/cloudscale.json (kept as code so it's reviewable)."""

import json
import pathlib

DS = {"type": "prometheus", "uid": "prometheus"}
_id = iter(range(1, 100))


def stat(title, expr, unit, x, desc, thresholds=None):
    return {"id": next(_id), "type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"h": 5, "w": 6, "x": x, "y": 0},
            "targets": [{"refId": "A", "expr": expr, "datasource": DS}],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area",
                        "textMode": "value"},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "noValue": "no data",
                                         "thresholds": {"mode": "absolute", "steps": thresholds or
                                                        [{"color": "blue", "value": None}]}}, "overrides": []}}


def ts(title, targets, unit, x, y, w=12, desc=""):
    return {"id": next(_id), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"h": 8, "w": w, "x": x, "y": y},
            "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": leg, "datasource": DS}
                        for i, (e, leg) in enumerate(targets)],
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 12, "lineWidth": 2}},
                            "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}


W = "$__range"
panels = [
    stat("LPT · p95 latency per task",
         f"max(histogram_quantile(0.95, sum by (le, agent) (rate(incident_task_latency_milliseconds_bucket[{W}]))))",
         "ms", 0, "Latency Per Task: p95 of the slowest agent node in the selected range"),
    stat("TCR · tokens / min", f"sum(rate(llm_tokens_total[{W}])) * 60", "short", 6,
         "Token Consumption Rate across all agents and models (0 in scripted mode)"),
    stat("CHR · cache hit ratio",
         f'sum(increase(cache_lookups_total{{result="hit"}}[{W}])) / sum(increase(cache_lookups_total[{W}]))',
         "percentunit", 12, "Cache Hit Ratio of the pgvector semantic cache"),
    stat("TFR · tool failure rate",
         f'sum(increase(mcp_tool_calls_total{{outcome="error"}}[{W}])) / sum(increase(mcp_tool_calls_total[{W}]))',
         "percentunit", 18, "Tool Failure Rate: failed MCP calls / all MCP calls (policy denials counted separately)",
         [{"color": "green", "value": None}, {"color": "orange", "value": 0.05}, {"color": "red", "value": 0.2}]),
    ts("Latency per agent (p95)",
       [("histogram_quantile(0.95, sum by (le, agent) (rate(incident_task_latency_milliseconds_bucket[5m])))",
         "{{agent}}")], "ms", 0, 5),
    ts("MCP tool calls by outcome",
       [("sum by (outcome) (increase(mcp_tool_calls_total[5m]))", "{{outcome}}")], "short", 12, 5),
    ts("Tokens by model",
       [("sum by (model, direction) (increase(llm_tokens_total[5m]))", "{{model}} {{direction}}")], "short", 0, 13),
    ts("LLM cost (USD, 5m)", [("sum by (model) (increase(llm_cost_usd_total[5m]))", "{{model}}")],
       "currencyUSD", 12, 13),
    ts("Semantic cache lookups", [("sum by (result) (increase(cache_lookups_total[5m]))", "{{result}}")],
       "short", 0, 21, 8),
    ts("HITL wait (p50 / p95)",
       [("histogram_quantile(0.5, sum by (le) (rate(hitl_wait_seconds_bucket[15m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(hitl_wait_seconds_bucket[15m])))", "p95")], "s", 8, 21, 8),
    ts("Circuit breakers (0 closed, 1 half-open, 2 open)",
       [("max by (tool, cloud) (circuit_state)", "{{tool}} {{cloud}}")], "short", 16, 21, 8),
]

dashboard = {
    "uid": "cloudscale", "title": "CloudScale Incident Platform", "tags": ["cloudscale", "capstone"],
    "timezone": "browser", "refresh": "10s", "schemaVersion": 39, "version": 1,
    "time": {"from": "now-1h", "to": "now"},
    "links": [{"title": "Traces in Jaeger (search incident.id)", "type": "link", "targetBlank": True,
               "url": "http://localhost:16686/search?service=orchestrator"}],
    "panels": panels,
}
out = pathlib.Path("observability/grafana/dashboards/cloudscale.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} with {len(panels)} panels")

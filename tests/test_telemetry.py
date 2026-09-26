"""Telemetry: instruments, zero-primed counters (so increase() sees the first event), trace context over _meta."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from cloudscale.common.gate import load_policy
from cloudscale.common.telemetry import Instruments, extract_context, prime


def _points(reader):
    out = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = [(dict(p.attributes), getattr(p, "value", None)) for p in m.data.data_points]
    return out


def test_counters_are_primed_at_zero_for_every_known_series():
    reader = InMemoryMetricReader()
    m = Instruments(MeterProvider(metric_readers=[reader]).get_meter("t"))
    prime(m)
    pts = _points(reader)
    tools = set(load_policy()["op_classes"])
    assert {(a["tool"], a["outcome"]) for a, _ in pts["mcp.tool.calls"]} == {
        (t, o) for t in tools for o in ("ok", "error", "denied")}
    assert {a["result"] for a, v in pts["cache.lookups"] if v == 0} == {"hit", "miss"}


def test_trace_context_round_trips_through_mcp_meta():
    tracer = TracerProvider().get_tracer("t")
    from opentelemetry import propagate, trace
    with tracer.start_as_current_span("client") as parent:
        carrier: dict[str, str] = {}
        propagate.inject(carrier, context=trace.set_span_in_context(parent))
    meta = {"io.cloudscale/token": "x", **carrier}          # what ToolClient sends as _meta
    ctx = extract_context(meta)
    child_parent = trace.get_current_span(ctx).get_span_context()
    assert child_parent.trace_id == parent.get_span_context().trace_id
    assert child_parent.span_id == parent.get_span_context().span_id

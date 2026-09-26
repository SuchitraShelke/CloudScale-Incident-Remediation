"""OpenTelemetry setup + the 7 metric instruments (4 jury metrics + 3 supporting).

  LPT  incident.task.latency (ms, histogram)    labels: agent, phase
  TCR  llm.tokens (counter)                     labels: direction(in/out), model, agent
  CHR  cache.lookups (counter)                  labels: result(hit/miss)
  TFR  mcp.tool.calls (counter)                 labels: tool, outcome(ok/error/denied)
       llm.cost.usd (counter)                   labels: model, agent
       hitl.wait.seconds (histogram)            labels: gate
       circuit.state (gauge 0 closed/1 half/2 open) labels: tool, cloud

Traces go over OTLP gRPC to Jaeger; metrics over OTLP HTTP to Prometheus' native receiver.
Trace context crosses the MCP hop inside the request `_meta` (the MCP client uses httpx2, which
OTel's httpx instrumentation doesn't see).
"""

import logging
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("cloudscale.telemetry")
CIRCUIT_VALUE = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}

tracer = trace.get_tracer("cloudscale")
_meter = metrics.get_meter("cloudscale")


class Instruments:
    def __init__(self, meter):
        self.task_latency = meter.create_histogram("incident.task.latency", unit="ms",
                                                   description="LPT: latency per agent task")
        self.llm_tokens = meter.create_counter("llm.tokens", description="TCR: tokens consumed")
        self.cache_lookups = meter.create_counter("cache.lookups", description="CHR: semantic cache lookups")
        self.tool_calls = meter.create_counter("mcp.tool.calls", description="TFR: MCP tool calls by outcome")
        self.llm_cost = meter.create_counter("llm.cost.usd", description="LLM spend")
        self.hitl_wait = meter.create_histogram("hitl.wait.seconds", unit="s", description="Human decision latency")
        self.circuit_state = meter.create_gauge("circuit.state", description="0 closed, 1 half-open, 2 open")


M = Instruments(_meter)   # no-op until setup() installs real providers (safe in tests)


def setup(service_name: str, endpoint: str, metrics_endpoint: str, export_interval_ms: int = 10_000) -> None:
    global M
    resource = Resource.create({"service.name": service_name, "service.namespace": "cloudscale"})
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(tp)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=metrics_endpoint),
                                           export_interval_millis=export_interval_ms)
    views = [  # default buckets stop at 10 s: the evaluator's polling and human decisions take longer
        View(instrument_name="incident.task.latency", aggregation=ExplicitBucketHistogramAggregation(
            [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 30000, 60000, 120000])),
        View(instrument_name="hitl.wait.seconds", aggregation=ExplicitBucketHistogramAggregation(
            [1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600])),
    ]
    provider = MeterProvider(resource=resource, metric_readers=[reader], views=views)
    metrics.set_meter_provider(provider)
    M = Instruments(metrics.get_meter("cloudscale"))
    prime(M)
    provider.force_flush(timeout_millis=5000)     # export the zeros now, before the first real event
    log.info("OpenTelemetry: traces -> %s, metrics -> %s (%s)", endpoint, metrics_endpoint, service_name)


def prime(m: Instruments) -> None:
    """Export every known counter series at 0 first. Prometheus' increase() needs an earlier sample,
    so a series that first appears already at 1 would hide that first hit / first tool failure."""
    from cloudscale.common.gate import load_policy
    for tool in load_policy()["op_classes"]:
        for outcome in ("ok", "error", "denied"):
            m.tool_calls.add(0, {"tool": tool, "outcome": outcome})
    for result in ("hit", "miss"):
        m.cache_lookups.add(0, {"result": result})


def instruments() -> Instruments:
    return M


def inject_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier                               # {"traceparent": "00-...", ...}


def extract_context(carrier: dict[str, Any] | None):
    return propagate.extract({k: v for k, v in (carrier or {}).items() if isinstance(v, str)})


@contextmanager
def span(name: str, **attrs: Any):
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield s

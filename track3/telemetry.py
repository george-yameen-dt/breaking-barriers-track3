"""One tracer provider for every span of a run, exported to CloudWatch, Dynatrace and Arize.

On AgentCore Runtime the agents start under `opentelemetry-instrument` (the AWS Distro for OpenTelemetry, ADOT). ADOT
builds the global tracer provider, instruments boto3 (each Bedrock model call becomes an OpenTelemetry GenAI `chat`
span) and exports every span to CloudWatch, where AgentCore Observability shows it. This module adds the other two
destinations to that same provider as mirrors. A mirror exports only the traces of runs that asked for it, so dev
runs and the runtime's own traffic stay in CloudWatch. On the laptop, without ADOT, it makes the provider itself.

The agents run on Strands, which traces each turn, model call and tool call with OpenTelemetry GenAI attributes
(Dynatrace and CloudWatch read those). One extra processor, Arize's StrandsAgentsToOpenInferenceProcessor, adds the
OpenInference attributes Arize reads, on the same spans, before any mirror exports them. It keeps the GenAI attributes
but drops GenAI span events, so Strands is told to put message content in attributes, not events (OPT_IN below).
"""

import contextlib
import os
import threading

# Read by Strands when it makes its tracer: latest GenAI conventions, content as span attributes (not events, which the
# OpenInference processor removes), and tool definitions on the model spans.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN",
                      "gen_ai_latest_experimental,gen_ai_span_attributes_only,gen_ai_tool_definitions")

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

_LOCK = threading.Lock()
_MIRRORS: dict = {}  # destination name -> _RunFilter
_CONVERTED: set = set()  # ids of the providers that already have the OpenInference processor


def provider() -> TracerProvider:
    """The global SDK tracer provider: ADOT's on AgentCore, otherwise one made here. The first call adds the processor
    that converts Strands spans for Arize, so it runs before every mirror added later."""
    with _LOCK:
        p = trace.get_tracer_provider()
        if not isinstance(p, TracerProvider):
            trace.set_tracer_provider(TracerProvider(resource=Resource.create({"service.name": "track3-checkpoint"})))
            p = trace.get_tracer_provider()
        if id(p) not in _CONVERTED:
            from openinference.instrumentation.strands_agents import StrandsAgentsToOpenInferenceProcessor

            p.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
            _CONVERTED.add(id(p))
        return p


def is_openinference(span) -> bool:
    """A span Arize can show: ours and Strands' carry an OpenInference kind; raw boto3 spans (ADOT's) do not."""
    return bool((span.attributes or {}).get("openinference.span.kind"))


class _RunTraceIds(RandomIdGenerator):
    """A run's root span takes the run's trace id, so every backend shows the id the audit records carry."""

    def __init__(self, trace_id: str):
        self._trace_id = int(trace_id, 16)

    def generate_trace_id(self) -> int:
        return self._trace_id


def tracer(trace_id: str):
    """A tracer whose root spans belong to this run's trace. The trace id in the scope gives each run its own tracer
    (the SDK caches tracers by scope), so the id generator can be set on that tracer alone."""
    t = provider().get_tracer("track3", attributes={"track3.trace_id": trace_id})
    t.id_generator = _RunTraceIds(trace_id)
    return t


class _RunFilter(SpanProcessor):
    """Passes on only the spans of the traces it was given (and, with keep, only the spans keep accepts)."""

    def __init__(self, inner: SpanProcessor, keep=None):
        self.inner, self.traces, self.keep = inner, set(), keep

    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span) -> None:
        if span.context.trace_id in self.traces and (self.keep is None or self.keep(span)):
            self.inner.on_end(span)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def mirror(name: str, trace_id: str, make_exporter, keep=None) -> None:
    """Sends this run's trace to the destination `name` too. The exporter is made the first time `name` is asked for."""
    provider()  # the OpenInference processor goes in before the first mirror
    with _LOCK:
        if name not in _MIRRORS:
            _MIRRORS[name] = _RunFilter(BatchSpanProcessor(make_exporter()), keep)
            new = _MIRRORS[name]
        else:
            new = None
        _MIRRORS[name].traces.add(int(trace_id, 16))
    if new:
        provider().add_span_processor(new)


class WithResource(SpanExporter):
    """Adds resource attributes on the way out, for a destination that routes by them (Arize reads its project there)."""

    def __init__(self, inner: SpanExporter, attributes: dict):
        self.inner, self.extra = inner, Resource(attributes)

    def export(self, spans):
        return self.inner.export([ReadableSpan(
            name=s.name, context=s.context, parent=s.parent, resource=s.resource.merge(self.extra), attributes=s.attributes,
            events=s.events, links=s.links, kind=s.kind, status=s.status, start_time=s.start_time, end_time=s.end_time,
            instrumentation_scope=s.instrumentation_scope) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def traceparent(span) -> str:
    """The W3C traceparent header for a span, so another service continues its trace under it."""
    ctx = span.get_span_context()
    # Only the sampled bit: the run's trace id is not random (it is the run's id), so the level-2 "random" flag would be wrong.
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags) & 0x01:02x}"


def flush() -> None:
    provider().force_flush()


def quiet():
    """Calls inside are not traced. For the checkpoint's own bookkeeping writes, which would otherwise add several
    boto3 spans per audit record. Without the instrumentation package (laptop, tests) nothing is instrumented anyway."""
    try:
        from opentelemetry.instrumentation.utils import suppress_instrumentation
    except ImportError:
        return contextlib.nullcontext()
    return suppress_instrumentation()

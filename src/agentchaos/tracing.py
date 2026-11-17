"""OpenTelemetry tracing: one trace per run, one span per proxied call or LLM call.

Off unless `setup_tracing` finds a reachable collector. Spans carry run and
step identifiers, status codes and token counts. They never carry request
headers, message contents or API keys. Span timestamps are wall-clock (set by
the SDK) and are not part of any canonical record.
"""

import logging
import socket
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

logger = logging.getLogger("agentchaos.tracing")
_provider: TracerProvider | None = None


def configure(provider: TracerProvider | None) -> None:
    """Use this tracer provider (tests pass an in-memory one; None turns tracing off)."""
    global _provider
    _provider = provider


def get_tracer() -> trace.Tracer:
    """A real tracer when configured, otherwise the API's no-op tracer."""
    if _provider is not None:
        return _provider.get_tracer("agentchaos")
    return trace.NoOpTracer()


def _reachable(endpoint: str, timeout_s: float = 0.5) -> bool:
    parsed = urlparse(endpoint)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout_s):
            return True
    except OSError:
        return False


def setup_tracing(endpoint: str | None, exporter: SpanExporter | None = None) -> bool:
    """Start exporting to `endpoint` if a collector answers there. Logs the reason if not."""
    if exporter is None:
        if not endpoint:
            logger.info("tracing disabled: no OTLP endpoint configured")
            return False
        if not _reachable(endpoint):
            logger.info("tracing disabled: no collector reachable at %s", endpoint)
            return False
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider = TracerProvider(resource=Resource.create({"service.name": "agentchaos"}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    configure(provider)
    logger.info("tracing enabled: exporting to %s", endpoint or "custom exporter")
    return True

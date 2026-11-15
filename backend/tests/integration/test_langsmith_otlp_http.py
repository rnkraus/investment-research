"""Exercise the installed OTLP exporter over loopback with synthetic spans."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic import SecretStr

from investment_research.infrastructure.operations import opentelemetry


def test_real_otlp_export_reaches_a_local_http_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, str | None, bytes]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("x-api-key"), body))
            self.send_response(200)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        monkeypatch.setattr(
            opentelemetry,
            "_LANGSMITH_ENDPOINT",
            f"http://127.0.0.1:{server.server_port}/otel/v1/traces",
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tracer = opentelemetry.create_operation_tracer(
                service_name="investment-research-api",
                service_version="1",
                langsmith_api_key=SecretStr("synthetic-loopback-key"),
            )
            try:
                with tracer.start_span("research.answer") as root:
                    root.set_attribute("outcome", "accepted")
                    with tracer.start_span("research.retrieve"):
                        pass
            finally:
                tracer.close()
        finally:
            server.shutdown()
            thread.join()

    [(path, key, body)] = received
    assert path == "/otel/v1/traces"
    assert key == "synthetic-loopback-key"
    message = ExportTraceServiceRequest.FromString(body)
    [resource_spans] = message.resource_spans
    [scope_spans] = resource_spans.scope_spans
    spans = {span.name: span for span in scope_spans.spans}
    assert set(spans) == {"research.answer", "research.retrieve"}
    assert spans["research.retrieve"].parent_span_id == spans["research.answer"].span_id

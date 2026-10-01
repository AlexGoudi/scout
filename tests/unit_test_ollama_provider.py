"""Offline tests for `OllamaProvider`, against a stub ollama server on loopback.

No model and no network: the stub is an `http.server` bound to 127.0.0.1 on a free port,
programmed per test with the answers a real ollama gives (and the ones it does not), and
recording every request body so the wire shape can be asserted exactly.

The proxy tests stand up a second stub that plays a corporate proxy — it records hits and
answers with an HTML error page — and point the proxy environment variables at it. That is
the trap this machine sets for any urllib client talking to 127.0.0.1.
"""

import dataclasses
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from scout_impl.cli import _parse_args
from scout_impl.ollama import (
    DEFAULT_KEEP_ALIVE,
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_NUM_THREAD,
    DEFAULT_OLLAMA_URL,
    DEFAULT_SEED,
    RESTART_COMMAND,
    OllamaError,
    OllamaModelMissing,
    OllamaProtocolError,
    OllamaProvider,
    OllamaTimeout,
    OllamaUnreachable,
    is_loopback,
    ollama_spec,
    resolve_base_url,
)
from scout_impl.provider import (
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    RESPONSE_FORMAT,
    Message,
    ProviderError,
    RecordingProvider,
    ReplayProvider,
    ToolSpec,
    create_provider,
)

HTML_PAGE = "<!DOCTYPE html><html><head><title>Proxy Error</title></head><body>Access denied</body></html>"
PROXY_VARIABLES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")

ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
GREP_TOOL = ToolSpec(
    name="grep",
    description="Search the repository",
    parameters={"type": "object", "properties": {"pattern": {"type": "string"}}},
)
MESSAGES = [Message(role="system", content="You are Scout."), Message(role="user", content="Is it covered?")]


@dataclasses.dataclass
class Reply:
    status: int = 200
    body: str = ""
    content_type: str = "application/json"
    delay_s: float = 0.0


def json_reply(payload: Any, status: int = 200) -> Reply:
    return Reply(status=status, body=json.dumps(payload))


def chat_payload(content: str = '{"answer": "yes"}', **overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": DEFAULT_MODEL,
        "created_at": "2026-09-24T11:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 2_500_000_000,
        "load_duration": 100_000_000,
        "prompt_eval_count": 42,
        "prompt_eval_duration": 300_000_000,
        "eval_count": 7,
        "eval_duration": 1_750_000_000,
    }
    payload.update(overrides)
    return payload


class StubServer:
    """A programmable HTTP server on loopback that records what it is sent."""

    def __init__(self, default: Optional[Reply] = None) -> None:
        self.routes: Dict[Tuple[str, str], Reply] = {}
        self.default = default or Reply(status=404, body="404 page not found", content_type="text/plain")
        self.requests: List[Tuple[str, str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                stub.requests.append((method, self.path, body))
                reply = stub.routes.get((method, self.path), stub.default)
                if reply.delay_s:
                    time.sleep(reply.delay_s)
                encoded = reply.body.encode("utf-8")
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", reply.content_type)
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # The client gave up first, which is exactly what the timeout test wants.
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() waits out one poll interval, and the default half second per test adds up.
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, method: str, path: str, reply: Reply) -> None:
        self.routes[(method, path)] = reply

    def bodies(self, path: str) -> List[Dict[str, Any]]:
        return [json.loads(body) for _, request_path, body in self.requests if request_path == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[StubServer]:
    server = StubServer()
    yield server
    server.close()


@pytest.fixture
def fake_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubServer]:
    """A corporate proxy stand-in, with the environment pointing every scheme at it."""
    proxy = StubServer(default=Reply(status=502, body=HTML_PAGE, content_type="text/html"))
    for variable in PROXY_VARIABLES:
        monkeypatch.setenv(variable, proxy.url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield proxy
    proxy.close()


def provider_for(url: str, **options: Any) -> OllamaProvider:
    return OllamaProvider(ollama_spec(), base_url=url, **options)


def closed_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# --- the request on the wire -------------------------------------------------------------


def test_the_request_carries_model_messages_options_and_keep_alive(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    provider = OllamaProvider(ollama_spec(max_output_tokens=64), base_url=stub.url, keep_alive="5m")

    provider.complete(MESSAGES)

    (body,) = stub.bodies("/api/chat")
    assert body["model"] == DEFAULT_MODEL
    assert body["messages"] == [message.to_dict() for message in MESSAGES]
    assert body["stream"] is False
    assert body["keep_alive"] == "5m"
    assert body["options"] == {
        "temperature": 0.0,
        "seed": DEFAULT_SEED,
        "num_predict": 64,
        "num_thread": DEFAULT_NUM_THREAD,
        "num_ctx": DEFAULT_NUM_CTX,
    }
    assert "format" not in body
    assert "tools" not in body


def test_the_context_window_is_pinned_rather_than_left_to_the_server(stub: StubServer) -> None:
    """Unset, ollama sizes the window from VRAM and silently truncates a longer prompt."""
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    provider = OllamaProvider(ollama_spec(max_output_tokens=256), base_url=stub.url, num_ctx=4096)

    provider.complete(MESSAGES)

    (body,) = stub.bodies("/api/chat")
    assert body["options"]["num_ctx"] == 4096
    assert provider.context_tokens == 4096 - 256


def test_the_defaults_are_the_measured_ones(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))

    provider_for(stub.url).complete(MESSAGES)

    (body,) = stub.bodies("/api/chat")
    assert body["keep_alive"] == DEFAULT_KEEP_ALIVE == "30m"
    assert body["options"]["num_thread"] == 16
    assert body["options"]["num_predict"] == 256


def test_response_format_becomes_format_and_other_tools_become_native_tools(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    response_format = ToolSpec(name=RESPONSE_FORMAT, parameters=ANSWER_SCHEMA)

    provider_for(stub.url).complete(MESSAGES, [response_format, GREP_TOOL])

    (body,) = stub.bodies("/api/chat")
    assert body["format"] == ANSWER_SCHEMA
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "grep", "description": "Search the repository", "parameters": GREP_TOOL.parameters},
        }
    ]


def test_a_response_format_alone_sends_no_tools(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))

    provider_for(stub.url).complete(MESSAGES, [ToolSpec(name=RESPONSE_FORMAT, parameters=ANSWER_SCHEMA)])

    (body,) = stub.bodies("/api/chat")
    assert body["format"] == ANSWER_SCHEMA
    assert "tools" not in body


# --- the answer ----------------------------------------------------------------------------


def test_tokens_come_from_the_server_accumulate_and_cost_nothing(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    provider = provider_for(stub.url)

    first = provider.complete(MESSAGES)
    provider.complete(MESSAGES)

    assert first.text == '{"answer": "yes"}'
    assert first.model_id == DEFAULT_MODEL
    assert first.finish_reason == FINISH_STOP
    assert (first.usage.input_tokens, first.usage.output_tokens) == (42, 7)
    assert (provider.usage.input_tokens, provider.usage.output_tokens) == (84, 14)
    assert provider.call_count == 2
    assert provider.usd == 0.0


def test_missing_counts_are_zero_and_missing_model_falls_back_to_the_spec(stub: StubServer) -> None:
    payload = chat_payload()
    for key in ("model", "prompt_eval_count", "eval_count"):
        del payload[key]
    stub.route("POST", "/api/chat", json_reply(payload))

    completion = provider_for(stub.url).complete(MESSAGES)

    assert completion.usage.total_tokens == 0
    assert completion.model_id == DEFAULT_MODEL


def test_timings_are_reported_in_seconds(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    provider = provider_for(stub.url)

    provider.complete(MESSAGES)

    assert provider.last_timings == pytest.approx(
        {"total_duration": 2.5, "load_duration": 0.1, "prompt_eval_duration": 0.3, "eval_duration": 1.75}
    )


def test_done_reason_length_is_finish_length(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload(content='{"answer": "trunc', done_reason="length")))

    assert provider_for(stub.url).complete(MESSAGES).finish_reason == FINISH_LENGTH


def test_native_tool_calls_are_parsed(stub: StubServer) -> None:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "grep", "arguments": {"pattern": "pg_profile"}}},
            {"function": {"name": "grep", "arguments": '{"pattern": "port_config"}'}},
        ],
    }
    stub.route("POST", "/api/chat", json_reply(chat_payload(message=message)))

    completion = provider_for(stub.url).complete(MESSAGES, [GREP_TOOL])

    assert completion.finish_reason == FINISH_TOOL_CALLS
    assert [(call.id, call.name, call.arguments) for call in completion.tool_calls] == [
        ("call-0", "grep", {"pattern": "pg_profile"}),
        ("call-1", "grep", {"pattern": "port_config"}),
    ]


# --- the proxy trap ------------------------------------------------------------------------


def test_a_plain_urlopen_to_loopback_is_routed_through_the_proxy(
    stub: StubServer, fake_proxy: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap itself: stock urllib sends a request for 127.0.0.1 to the proxy."""
    stub.route("GET", "/api/tags", json_reply({"models": []}))
    # urlopen caches its opener, and with it the proxy environment of whenever it was
    # first called; clearing the cache makes it read the environment this test set.
    monkeypatch.setattr(urllib.request, "_opener", None)

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{stub.url}/api/tags", timeout=5)

    assert error.value.code == 502
    assert "<html" in error.value.read().decode("utf-8")
    assert len(fake_proxy.requests) == 1
    assert fake_proxy.requests[0][1] == f"{stub.url}/api/tags"
    assert stub.requests == []


def test_the_provider_bypasses_the_proxy_for_loopback(stub: StubServer, fake_proxy: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply(chat_payload()))
    stub.route("GET", "/api/tags", json_reply({"models": [{"name": DEFAULT_MODEL}]}))
    provider = provider_for(stub.url)

    provider.preflight()
    completion = provider.complete(MESSAGES)

    assert completion.text == '{"answer": "yes"}'
    assert [path for _, path, _ in stub.requests] == ["/api/tags", "/api/chat"]
    assert fake_proxy.requests == []


def test_a_non_loopback_url_honours_the_proxy_environment(fake_proxy: StubServer) -> None:
    """The other half of the rule: a remote ollama goes wherever the environment says."""
    provider = provider_for("http://ollama.scout-test.invalid:11434")

    with pytest.raises(OllamaProtocolError) as error:
        provider.complete(MESSAGES)

    assert len(fake_proxy.requests) == 1
    assert fake_proxy.requests[0][1] == "http://ollama.scout-test.invalid:11434/api/chat"
    assert "proxy" in str(error.value)


# --- failures, each distinct and each saying what to do ------------------------------------


def test_a_closed_port_is_unreachable_and_says_how_to_start_a_server() -> None:
    url = f"http://127.0.0.1:{closed_port()}"

    with pytest.raises(OllamaUnreachable) as error:
        provider_for(url).complete(MESSAGES)

    message = str(error.value)
    assert url in message
    assert RESTART_COMMAND in message
    assert (
        "OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS=$HOME/.ollama/models OLLAMA_KEEP_ALIVE=30m ollama serve"
    ) in message
    assert "SCOUT_OLLAMA_URL" in message and "--ollama-url" in message


def test_a_model_not_found_404_says_how_to_pull_it(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply({"error": f"model '{DEFAULT_MODEL}' not found"}, status=404))

    with pytest.raises(OllamaModelMissing) as error:
        provider_for(stub.url).complete(MESSAGES)

    host_port = stub.url[len("http://"):]
    assert DEFAULT_MODEL in str(error.value)
    assert f"OLLAMA_HOST={host_port} ollama pull {DEFAULT_MODEL}" in str(error.value)


def test_a_404_that_is_not_about_the_model_is_a_protocol_error(stub: StubServer) -> None:
    """A wrong path gets ollama's plain-text 404, which also says "not found"."""
    with pytest.raises(OllamaProtocolError) as error:
        provider_for(stub.url).complete(MESSAGES)

    assert "404" in str(error.value)
    assert "not JSON" in str(error.value)


def test_a_per_call_timeout_lowers_the_provider_timeout(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", Reply(body=json.dumps(chat_payload()), delay_s=1.0))

    started = time.monotonic()
    with pytest.raises(OllamaTimeout) as error:
        provider_for(stub.url, timeout_s=3000).complete(MESSAGES, timeout_s=0.2)

    assert time.monotonic() - started < 1.0
    assert "within 0.2s" in str(error.value)


def test_a_slow_answer_times_out_and_says_what_to_change(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", Reply(body=json.dumps(chat_payload()), delay_s=1.0))

    started = time.monotonic()
    with pytest.raises(OllamaTimeout) as error:
        provider_for(stub.url, timeout_s=0.2).complete(MESSAGES)

    assert time.monotonic() - started < 1.0
    message = str(error.value)
    assert "0.2s" in message
    assert "shared" in message
    assert "timeout_s" in message and "num_predict" in message


def test_an_html_body_is_a_protocol_error_that_names_the_proxy(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", Reply(body=HTML_PAGE, content_type="text/html"))

    with pytest.raises(OllamaProtocolError) as error:
        provider_for(stub.url).complete(MESSAGES)

    message = str(error.value)
    assert "not JSON" in message
    assert "<!DOCTYPE html><html><head><title>Proxy Error" in message
    assert "proxy" in message.lower()
    assert "HTTP_PROXY" in message


def test_a_server_error_is_a_protocol_error_with_its_status(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply({"error": "llama runner process has terminated"}, status=500))

    with pytest.raises(OllamaProtocolError) as error:
        provider_for(stub.url).complete(MESSAGES)

    assert "HTTP 500" in str(error.value)
    assert "llama runner process has terminated" in str(error.value)


def test_json_of_the_wrong_shape_is_a_protocol_error(stub: StubServer) -> None:
    stub.route("POST", "/api/chat", json_reply({"done": True}))

    with pytest.raises(OllamaProtocolError):
        provider_for(stub.url).complete(MESSAGES)


def test_every_failure_is_a_provider_error() -> None:
    for error in (OllamaUnreachable, OllamaModelMissing, OllamaTimeout, OllamaProtocolError):
        assert issubclass(error, OllamaError)
        assert issubclass(error, ProviderError)


# --- where the server is -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("explicit", "environ", "expected"),
    [
        ("http://127.0.0.1:1", {"SCOUT_OLLAMA_URL": "http://127.0.0.1:2", "OLLAMA_HOST": "127.0.0.1:3"},
         "http://127.0.0.1:1"),
        (None, {"SCOUT_OLLAMA_URL": "http://127.0.0.1:2", "OLLAMA_HOST": "127.0.0.1:3"}, "http://127.0.0.1:2"),
        (None, {"OLLAMA_HOST": "127.0.0.1:3"}, "http://127.0.0.1:3"),
        (None, {}, DEFAULT_OLLAMA_URL),
        ("", {"SCOUT_OLLAMA_URL": "  ", "OLLAMA_HOST": "127.0.0.1:3"}, "http://127.0.0.1:3"),
    ],
)
def test_resolve_base_url_precedence(explicit: Optional[str], environ: Dict[str, str], expected: str) -> None:
    assert resolve_base_url(explicit, environ) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:11435", "http://127.0.0.1:11435"),
        ("http://127.0.0.1:11435/", "http://127.0.0.1:11435"),
        ("0.0.0.0:11435", "http://127.0.0.1:11435"),
        ("0.0.0.0", "http://127.0.0.1:11434"),
        ("localhost", "http://localhost:11434"),
        ("https://ollama.example.com", "https://ollama.example.com:11434"),
        ("http://gpu-box:8080/ollama/", "http://gpu-box:8080/ollama"),
        ("[::1]:11435", "http://[::1]:11435"),
    ],
)
def test_resolve_base_url_normalizes(raw: str, expected: str) -> None:
    assert resolve_base_url(raw, environ={}) == expected


def test_the_provider_resolves_its_url_from_the_environ_it_is_given() -> None:
    provider = OllamaProvider(ollama_spec(), environ={"OLLAMA_HOST": "0.0.0.0:11435"})

    assert provider.base_url == "http://127.0.0.1:11435"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://127.1.2.3:11434", True),
        ("http://[::1]:11434", True),
        ("http://localhost:11434", True),
        ("127.0.0.1:11435", True),
        ("http://10.0.0.5:11434", False),
        ("http://ollama.example.com:11434", False),
        ("http://[2001:db8::1]:11434", False),
    ],
)
def test_is_loopback(url: str, expected: bool) -> None:
    assert is_loopback(url) is expected


# --- preflight -----------------------------------------------------------------------------


def test_preflight_passes_when_the_model_is_there(stub: StubServer) -> None:
    stub.route("GET", "/api/tags", json_reply({"models": [{"name": "other:1b"}, {"name": DEFAULT_MODEL}]}))

    provider_for(stub.url).preflight()


def test_preflight_accepts_a_model_listed_under_latest(stub: StubServer) -> None:
    stub.route("GET", "/api/tags", json_reply({"models": [{"name": "llama3:latest", "model": "llama3:latest"}]}))

    OllamaProvider(ollama_spec("llama3"), base_url=stub.url).preflight()


def test_preflight_names_a_missing_model_and_how_to_pull_it(stub: StubServer) -> None:
    stub.route("GET", "/api/tags", json_reply({"models": [{"name": "other:1b"}]}))

    with pytest.raises(OllamaModelMissing) as error:
        provider_for(stub.url).preflight()

    assert DEFAULT_MODEL in str(error.value)
    assert "other:1b" in str(error.value)
    assert f"ollama pull {DEFAULT_MODEL}" in str(error.value)


def test_preflight_on_an_empty_server_says_so(stub: StubServer) -> None:
    stub.route("GET", "/api/tags", json_reply({"models": []}))

    with pytest.raises(OllamaModelMissing) as error:
        provider_for(stub.url).preflight()

    assert "no models" in str(error.value)


def test_preflight_against_a_closed_port_is_unreachable() -> None:
    with pytest.raises(OllamaUnreachable):
        provider_for(f"http://127.0.0.1:{closed_port()}").preflight()


# --- integration with the rest of the provider layer ---------------------------------------


def test_ollama_spec_is_pinned_deterministic_and_free() -> None:
    spec = ollama_spec()

    assert (spec.provider, spec.model_id, spec.temperature) == ("ollama", DEFAULT_MODEL, 0.0)
    assert spec.max_output_tokens == 256
    assert (spec.input_usd_per_1k, spec.output_usd_per_1k) == (0.0, 0.0)


def test_create_provider_builds_an_ollama_provider(stub: StubServer) -> None:
    provider = create_provider("ollama", ollama_spec(), base_url=stub.url, timeout_s=5.0)

    assert isinstance(provider, OllamaProvider)
    assert provider.base_url == stub.url
    assert provider.timeout_s == 5.0


def test_a_recorded_answer_replays_identically_with_the_server_gone(tmp_path: Path) -> None:
    server = StubServer()
    server.route("POST", "/api/chat", json_reply(chat_payload()))
    tools = [ToolSpec(name=RESPONSE_FORMAT, parameters=ANSWER_SCHEMA)]
    try:
        recorded = RecordingProvider(provider_for(server.url), tmp_path).complete(MESSAGES, tools)
    finally:
        server.close()

    replayed = ReplayProvider.from_fixtures(tmp_path).complete(MESSAGES, tools)

    assert replayed.cached is True
    assert dataclasses.replace(replayed, cached=False) == recorded


def test_the_demos_and_the_cli_default_to_the_same_model() -> None:
    root = Path(__file__).resolve().parent.parent
    demo_defaults = {
        name: re.findall(r"SCOUT_OLLAMA_MODEL:-([^}]+)\}", (root / name).read_text())
        for name in ("demo.sh", "demo-env.sh", "demo-serve.sh")
    }
    assert all(demo_defaults.values()), demo_defaults
    assert {model for models in demo_defaults.values() for model in models} == {DEFAULT_MODEL}
    assert _parse_args(["review", "--range", "a..b"]).model == DEFAULT_MODEL

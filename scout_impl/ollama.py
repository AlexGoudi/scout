"""A live `Provider` backed by a local ollama server, over its native HTTP API.

One POST to `/api/chat` per `complete`, with `stream: false`, through nothing but
`urllib`: no SDK and no dependency, the same rule `scout_impl/provider.py` sets for every
live provider. Token accounting comes from the server's own `prompt_eval_count` and
`eval_count`, and cost is zero because a local model costs nothing to call.

The `RESPONSE_FORMAT` convention maps onto ollama's `format` field, which constrains the
whole answer to a JSON schema. Every other `ToolSpec` becomes a native ollama tool, and the
`tool_calls` the model returns become `ToolCall`s.

Where the server is. `resolve_base_url` takes, in order, an explicit URL (the CLI's
`--ollama-url`), `$SCOUT_OLLAMA_URL`, `$OLLAMA_HOST` (ollama's own variable, usually a bare
`host:port`), and ollama's default `http://127.0.0.1:11434`. On this machine the default is
the wrong answer: the system service on 11434 has no models and cannot download any, and
the models live under a user-level server on `http://127.0.0.1:11435` serving
`qwen2.5:7b-instruct`. Point `SCOUT_OLLAMA_URL` (or `--ollama-url`) at 11435.

The proxy trap. `HTTP_PROXY`, `HTTPS_PROXY` and `http_proxy` are set here for a corporate
proxy, and urllib honours them even for 127.0.0.1 whenever `NO_PROXY` does not name it —
and here it names 127.0.1.1, not 127.0.0.1. A naive client therefore sends a loopback
request to the proxy and gets its HTML error page back. So a loopback base URL gets an
opener with no proxy handling at all, and only a non-loopback one honours the environment.

Speed, measured on this machine against `qwen2.5:7b-instruct`. With ollama's default
thread count output ran at 1.7 tokens/s, 108 s for a single answer. With
`options.num_thread=16` and `num_predict` capped it ran at about 23 tokens/s, roughly 13 s
per question, and prompt evaluation at about 140 tokens/s. Hence the defaults: 16 threads,
256 output tokens, a 300 s timeout for a box that is shared, and `keep_alive` of 30 minutes
so the model is not reloaded between questions of one run.

The context window is pinned too. Left unset, ollama sizes it from the VRAM it finds, 4096
tokens on this GPU-less host and more elsewhere, and a prompt longer than the window is
truncated from the front without an error. So `num_ctx` is always sent, and `context_tokens`
tells a caller how much prompt fits before it sends one.

Determinism (NFR-3). Temperature 0 and a fixed seed. The seed, `num_thread` and `num_ctx`
are not part of `ModelSpec.identity()` and so not part of the replay key; `num_predict` and
temperature are, through `max_output_tokens` and `temperature`.
"""

import ipaddress
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from typing import Any, Dict, List, Mapping, Optional

from .provider import (
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    RESPONSE_FORMAT,
    Completion,
    Message,
    ModelSpec,
    Provider,
    ProviderError,
    TokenUsage,
    ToolCall,
    ToolSpec,
    register_provider,
)

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_URL_ENV = "SCOUT_OLLAMA_URL"
OLLAMA_HOST_ENV = "OLLAMA_HOST"
DEFAULT_OLLAMA_PORT = 11434
DEFAULT_MODEL = "qwen2.5:7b-instruct"
DEFAULT_TIMEOUT_S = 3000.0
DEFAULT_NUM_THREAD = 16
DEFAULT_SEED = 20260924
DEFAULT_MAX_OUTPUT_TOKENS = 256
DEFAULT_NUM_CTX = 8192
DEFAULT_KEEP_ALIVE = "30m"

RESTART_COMMAND = "OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS=$HOME/.ollama/models OLLAMA_KEEP_ALIVE=30m ollama serve"

_SNIPPET_CHARS = 120
_NANOSECONDS = 1e9
_TIMING_FIELDS = ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
_BIND_ALL_HOSTS = {"0.0.0.0": "127.0.0.1", "::": "::1"}


class OllamaError(ProviderError):
    """An ollama call failed. Subclasses say how, so a caller can act on it."""


class OllamaUnreachable(OllamaError):
    """Nothing answered at the base URL: connection refused, no route, or no such host."""


class OllamaModelMissing(OllamaError):
    """The server answered but does not have the model pulled."""


class OllamaTimeout(OllamaError):
    """The call took longer than the provider's timeout."""


class OllamaProtocolError(OllamaError):
    """The server answered with something other than the JSON ollama speaks."""


def resolve_base_url(explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """Where the ollama server is: explicit, `$SCOUT_OLLAMA_URL`, `$OLLAMA_HOST`, then the default.

    `OLLAMA_HOST` is what `ollama serve` binds to, so it is usually a bare `host:port` and
    may be the bind-all address, which is a place to listen on but not one to connect to.
    """
    environ = os.environ if environ is None else environ
    candidates = (explicit, environ.get(OLLAMA_URL_ENV), environ.get(OLLAMA_HOST_ENV))
    raw = next((value.strip() for value in candidates if value and value.strip()), DEFAULT_OLLAMA_URL)
    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urllib.parse.urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    host = _BIND_ALL_HOSTS.get(host, host)
    try:
        port = parts.port or DEFAULT_OLLAMA_PORT
    except ValueError as error:
        raise ValueError(f"Not an ollama URL Scout can use: {raw!r} ({error})") from None
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def is_loopback(url: str) -> bool:
    """True when `url` names this machine: 127.0.0.0/8, ::1 or localhost."""
    candidate = url if "://" in url else f"http://{url}"
    host = urllib.parse.urlsplit(candidate).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ollama_spec(model_id: str = DEFAULT_MODEL, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> ModelSpec:
    """The pinned spec for a local model, which costs nothing per token."""
    return ModelSpec(
        provider="ollama",
        model_id=model_id,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        input_usd_per_1k=0.0,
        output_usd_per_1k=0.0,
    )


def _snippet(text: str) -> str:
    return " ".join(text.split())[:_SNIPPET_CHARS]


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return head.startswith("<!doctype") or "<html" in head


def _is_timeout(reason: Any) -> bool:
    return isinstance(reason, (socket.timeout, TimeoutError))


class OllamaProvider(Provider):
    """`Provider` over one ollama server's `/api/chat`."""

    def __init__(
        self,
        spec: ModelSpec,
        base_url: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        num_thread: int = DEFAULT_NUM_THREAD,
        seed: int = DEFAULT_SEED,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
        num_ctx: int = DEFAULT_NUM_CTX,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(spec)
        self._base_url = resolve_base_url(base_url, environ)
        self.timeout_s = timeout_s
        self.num_thread = num_thread
        self.seed = seed
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.last_timings: Dict[str, float] = {}
        if is_loopback(self._base_url):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def _host_port(self) -> str:
        return urllib.parse.urlsplit(self._base_url).netloc

    @property
    def context_tokens(self) -> int:
        """Prompt tokens that fit in the pinned window with room left for the answer."""
        return max(0, self.num_ctx - self._spec.max_output_tokens)

    def preflight(self) -> None:
        """Check the server answers and has the model, before any question is spent on it."""
        payload = self._request("GET", "/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise OllamaProtocolError(
                f"{self._base_url}/api/tags answered without a model list: {_snippet(json.dumps(payload))}"
            )

        names = set()
        for entry in models:
            if isinstance(entry, dict):
                names.update(str(entry[key]) for key in ("name", "model") if entry.get(key))
        wanted = self._spec.model_id
        if wanted in names or f"{wanted}:latest" in names:
            return
        raise self._model_missing(f"it has {sorted(names) or 'no models at all'}")

    def _complete(self, messages: List[Message], tools: List[ToolSpec]) -> Completion:
        payload = self._request("POST", "/api/chat", self._chat_body(messages, tools))
        message = payload.get("message")
        if not isinstance(message, dict):
            raise OllamaProtocolError(
                f"{self._base_url}/api/chat answered without a message: {_snippet(json.dumps(payload))}"
            )

        self.last_timings = {
            name: float(payload[name]) / _NANOSECONDS
            for name in _TIMING_FIELDS
            if isinstance(payload.get(name), (int, float))
        }
        tool_calls = [self._tool_call(index, raw) for index, raw in enumerate(message.get("tool_calls") or [])]
        if payload.get("done_reason") == "length":
            finish_reason = FINISH_LENGTH
        elif tool_calls:
            finish_reason = FINISH_TOOL_CALLS
        else:
            finish_reason = FINISH_STOP

        return Completion(
            text=str(message.get("content") or ""),
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=int(payload.get("prompt_eval_count") or 0),
                output_tokens=int(payload.get("eval_count") or 0),
            ),
            model_id=str(payload.get("model") or self._spec.model_id),
            finish_reason=finish_reason,
        )

    def _chat_body(self, messages: List[Message], tools: List[ToolSpec]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self._spec.model_id,
            "messages": [message.to_dict() for message in messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self._spec.temperature,
                "seed": self.seed,
                "num_predict": self._spec.max_output_tokens,
                "num_thread": self.num_thread,
                "num_ctx": self.num_ctx,
            },
        }

        formats = [tool for tool in tools if tool.name == RESPONSE_FORMAT]
        if len(formats) > 1:
            raise ProviderError(f"At most one {RESPONSE_FORMAT!r} spec per request, got {len(formats)}")
        if formats:
            # ollama reads the string "json" as "any JSON object", which is the only honest
            # reading of a response format that declares no schema.
            body["format"] = formats[0].parameters or "json"

        native = [
            {
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
            }
            for tool in tools
            if tool.name != RESPONSE_FORMAT
        ]
        if native:
            body["tools"] = native
        return body

    def _tool_call(self, index: int, raw: Any) -> ToolCall:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            raise OllamaProtocolError(f"{self._base_url} returned a tool call Scout cannot read: {_snippet(str(raw))}")

        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                raise OllamaProtocolError(
                    f"{self._base_url} returned tool call {function['name']!r} with arguments that are not "
                    f"JSON: {_snippet(arguments)}"
                ) from None
        if not isinstance(arguments, dict):
            raise OllamaProtocolError(
                f"{self._base_url} returned tool call {function['name']!r} with non-object arguments: "
                f"{_snippet(str(arguments))}"
            )
        return ToolCall(id=f"call-{index}", name=str(function["name"]), arguments=arguments)

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One HTTP exchange, with every way it can fail turned into an `OllamaError`."""
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})

        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                status = response.status
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            try:
                text = error.read().decode("utf-8", errors="replace")
            except (OSError, HTTPException):
                text = ""
            raise self._http_error(url, error.code, text) from None
        except urllib.error.URLError as error:
            if _is_timeout(error.reason):
                raise self._timeout(url, time.monotonic() - started) from None
            raise self._unreachable(url, error.reason) from None
        except (socket.timeout, TimeoutError):
            raise self._timeout(url, time.monotonic() - started) from None
        except HTTPException as error:
            raise OllamaProtocolError(f"{url} did not speak HTTP: {type(error).__name__}: {error}") from None
        except OSError as error:
            raise self._unreachable(url, error) from None
        logger.debug("%s %s answered %d in %.2fs", method, url, status, time.monotonic() - started)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise self._not_json(url, status, text) from None
        if not isinstance(payload, dict):
            raise OllamaProtocolError(f"{url} answered JSON that is not an object: {_snippet(text)}")
        if payload.get("error"):
            raise OllamaProtocolError(f"{url} answered HTTP {status} with an error: {_snippet(str(payload['error']))}")
        return payload

    def _http_error(self, url: str, status: int, text: str) -> OllamaError:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self._not_json(url, status, text)

        message = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        lowered = message.lower()
        # Only ollama's own JSON answer counts as "model missing": a bare 404 from a wrong
        # path or a reverse proxy also says "not found" and means something else entirely.
        if status == 404 and "model" in lowered and "not found" in lowered:
            return self._model_missing(f"it said: {message}")
        return OllamaProtocolError(f"{url} answered HTTP {status}: {_snippet(message or text)}")

    def _not_json(self, url: str, status: int, text: str) -> OllamaProtocolError:
        hint = ""
        if _looks_like_html(text):
            hint = (
                " The body is HTML, which is what a proxy's error page looks like, not ollama. If HTTP_PROXY or "
                "HTTPS_PROXY is set, the request was probably routed through the proxy: Scout bypasses proxies "
                "only for loopback URLs, so either run ollama on this machine or add its host to NO_PROXY."
            )
        return OllamaProtocolError(
            f"{url} answered HTTP {status} with a body that is not JSON: {_snippet(text)!r}.{hint}"
        )

    def _unreachable(self, url: str, reason: Any) -> OllamaUnreachable:
        return OllamaUnreachable(
            f"Nothing answered at {url} ({reason}). Start an ollama server, on this machine with: "
            f"{RESTART_COMMAND} — then point Scout at it with ${OLLAMA_URL_ENV} or --ollama-url "
            f"(currently {self._base_url})."
        )

    def _model_missing(self, detail: str) -> OllamaModelMissing:
        return OllamaModelMissing(
            f"The ollama server at {self._base_url} does not have model {self._spec.model_id!r}; {detail}. "
            f"Pull it with: OLLAMA_HOST={self._host_port} ollama pull {self._spec.model_id}"
        )

    def _timeout(self, url: str, elapsed_s: float) -> OllamaTimeout:
        return OllamaTimeout(
            f"{url} did not answer within {self.timeout_s:g}s (gave up after {elapsed_s:.1f}s). A shared machine "
            f"can be slow to generate: raise timeout_s, or cap num_predict with a smaller max_output_tokens "
            f"(currently {self._spec.max_output_tokens})."
        )


register_provider("ollama", lambda spec, **options: OllamaProvider(spec, **options))

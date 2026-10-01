"""Model provider abstraction and the offline replay implementation.

`Provider.complete` is the only entry point call sites use. A live provider, such as
`ollama.OllamaProvider`, subclasses `Provider` and registers a factory under a name;
nothing that calls `complete` has to change, and no provider SDK is a dependency of this
package.

`ReplayProvider` serves recorded responses addressed by `(prompt_sha, model_id,
input_hash)`, which is what makes the backtest and these unit tests
hermetic and free (NFR-10). `RecordingProvider` captures a live provider's responses into
that same on-disk layout so they can be replayed afterwards.
"""

import abc
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

FIXTURE_VERSION = "1.0"

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

FINISH_STOP = "stop"
FINISH_TOOL_CALLS = "tool_calls"
FINISH_LENGTH = "length"

# A `ToolSpec` under this name is not a tool. Its `parameters` are the JSON schema the
# whole response must satisfy, for a provider that supports structured output. It rides
# in `tools` so it takes part in the request key: a replay can never serve an answer that
# was recorded under a different schema.
RESPONSE_FORMAT = "response_format"


class ProviderError(RuntimeError):
    """Base class for provider failures; the orchestrator degrades rather than fails (NFR-7)."""


class ReplayMiss(ProviderError):
    """No recorded response exists for the request."""


@dataclass(frozen=True)
class Message:
    """One turn of the conversation sent to the model."""

    role: str
    content: str
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Message":
        return cls(
            role=str(payload["role"]),
            content=str(payload.get("content") or ""),
            name=payload.get("name"),
        )


@dataclass(frozen=True)
class ToolSpec:
    """Declaration of a read-only repo tool offered to the model."""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolSpec":
        return cls(
            name=str(payload["name"]),
            description=str(payload.get("description") or ""),
            parameters=dict(payload.get("parameters") or {}),
        )


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            arguments=dict(payload.get("arguments") or {}),
        )


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting for the budget governor and the report's cost block."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TokenUsage":
        return cls(
            input_tokens=int(payload.get("input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
        )


@dataclass(frozen=True)
class ModelSpec:
    """Pinned model identity and sampling parameters.

    Temperature defaults to 0 and `model_id` must name a pinned version rather than a
    floating alias, because NFR-3 makes determinism a requirement rather than a nicety.
    """

    provider: str
    model_id: str
    temperature: float = 0.0
    max_output_tokens: int = 4096
    input_usd_per_1k: float = 0.0
    output_usd_per_1k: float = 0.0

    def __post_init__(self) -> None:
        if not self.provider or not self.model_id:
            raise ValueError("ModelSpec requires both a provider and a pinned model_id")

    def usd(self, usage: TokenUsage) -> float:
        return (
            usage.input_tokens * self.input_usd_per_1k
            + usage.output_tokens * self.output_usd_per_1k
        ) / 1000.0

    def identity(self) -> Dict[str, Any]:
        """The subset of the spec that changes a response, and therefore the cache key."""
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "input_usd_per_1k": self.input_usd_per_1k,
            "output_usd_per_1k": self.output_usd_per_1k,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ModelSpec":
        return cls(
            provider=str(payload["provider"]),
            model_id=str(payload["model_id"]),
            temperature=float(payload.get("temperature", 0.0)),
            max_output_tokens=int(payload.get("max_output_tokens", 4096)),
            input_usd_per_1k=float(payload.get("input_usd_per_1k", 0.0)),
            output_usd_per_1k=float(payload.get("output_usd_per_1k", 0.0)),
        )


@dataclass(frozen=True)
class Completion:
    """What every provider returns, live or replayed."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model_id: str = ""
    finish_reason: str = FINISH_STOP
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "usage": self.usage.to_dict(),
            "model_id": self.model_id,
            "finish_reason": self.finish_reason,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Completion":
        return cls(
            text=str(payload.get("text") or ""),
            tool_calls=[ToolCall.from_dict(item) for item in payload.get("tool_calls") or []],
            usage=TokenUsage.from_dict(payload.get("usage") or {}),
            model_id=str(payload.get("model_id") or ""),
            finish_reason=str(payload.get("finish_reason") or FINISH_STOP),
        )


@dataclass(frozen=True)
class RequestKey:
    """Content address of one request: the replay and cache key."""

    prompt_sha: str
    input_hash: str
    model_id: str
    digest: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_sha": self.prompt_sha,
            "input_hash": self.input_hash,
            "model_id": self.model_id,
            "digest": self.digest,
        }


def _canonical_sha(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_key(
    spec: ModelSpec,
    messages: Sequence[Message],
    tools: Optional[Sequence[ToolSpec]] = None,
) -> RequestKey:
    """Derive the content address of a request.

    The system turns are hashed separately from the rest because they carry the pinned
    prompt that NFR-3 requires be reported per run, and the split lets a prompt change be
    told apart from an input change when a replay misses.
    """
    system_turns = [message.to_dict() for message in messages if message.role == ROLE_SYSTEM]
    other_turns = [message.to_dict() for message in messages if message.role != ROLE_SYSTEM]
    tool_specs = [tool.to_dict() for tool in tools or []]

    prompt_sha = _canonical_sha(system_turns)
    input_hash = _canonical_sha({"messages": other_turns, "tools": tool_specs})
    digest = _canonical_sha(
        {"model": spec.identity(), "prompt_sha": prompt_sha, "input_hash": input_hash}
    )
    return RequestKey(
        prompt_sha=prompt_sha,
        input_hash=input_hash,
        model_id=spec.model_id,
        digest=digest,
    )


class Provider(abc.ABC):
    """Uniform interface over one live provider plus the replay provider.

    Subclasses implement `_complete`; `complete` owns the token accounting so an
    implementation cannot forget to report what it spent.
    """

    def __init__(self, spec: ModelSpec) -> None:
        self._spec = spec
        self._usage = TokenUsage()
        self._call_count = 0

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def usd(self) -> float:
        return self._spec.usd(self._usage)

    def preflight(self) -> None:
        """Raise a `ProviderError` if this provider cannot answer, before a question is spent on it."""

    def complete(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        timeout_s: Optional[float] = None,
    ) -> Completion:
        """One model call. `timeout_s`, when given, caps this call below the provider's own timeout."""
        if not messages:
            raise ProviderError("complete() requires at least one message")

        completion = self._complete(list(messages), list(tools or []), timeout_s)
        self._usage = self._usage + completion.usage
        self._call_count += 1
        return completion

    @abc.abstractmethod
    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        raise NotImplementedError


class ReplayProvider(Provider):
    """Serves recorded responses from `fixture_dir`. Makes no network calls, ever."""

    def __init__(self, fixture_dir: Path, spec: ModelSpec) -> None:
        super().__init__(spec)
        self.fixture_dir = Path(fixture_dir)

    @classmethod
    def from_fixtures(cls, fixture_dir: Path) -> "ReplayProvider":
        """Build a provider pinned to the model the fixtures were recorded against."""
        fixture_dir = Path(fixture_dir)
        specs: Dict[str, ModelSpec] = {}
        for path in sorted(fixture_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            spec = ModelSpec.from_dict(payload["model"])
            specs[spec.model_id] = spec

        if not specs:
            raise ProviderError(f"No replay fixtures found in {fixture_dir}")
        if len(specs) > 1:
            raise ProviderError(f"Fixtures in {fixture_dir} mix models: {sorted(specs)}")

        return cls(fixture_dir=fixture_dir, spec=next(iter(specs.values())))

    def preflight(self) -> None:
        if not self.fixture_dir.is_dir():
            raise ProviderError(f"Replay fixture directory {self.fixture_dir} does not exist")

    def fixture_path(self, key: RequestKey) -> Path:
        return self.fixture_dir / f"{key.digest}.json"

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        key = request_key(self._spec, messages, tools)
        path = self.fixture_path(key)
        if not path.is_file():
            raise ReplayMiss(
                f"No fixture {key.digest[:12]} in {self.fixture_dir} "
                f"(prompt_sha={key.prompt_sha[:12]}, input_hash={key.input_hash[:12]}, "
                f"{len(list(self.fixture_dir.glob('*.json')))} fixture(s) present)"
            )

        payload = json.loads(path.read_text(encoding="utf-8"))
        recorded = Completion.from_dict(payload["response"])
        return Completion(
            text=recorded.text,
            tool_calls=recorded.tool_calls,
            usage=recorded.usage,
            model_id=recorded.model_id or self._spec.model_id,
            finish_reason=recorded.finish_reason,
            cached=True,
        )


class RecordingProvider(Provider):
    """Wraps a provider and writes each response into a replay fixture directory."""

    def __init__(self, delegate: Provider, fixture_dir: Path) -> None:
        super().__init__(delegate.spec)
        self.delegate = delegate
        self.fixture_dir = Path(fixture_dir)

    def preflight(self) -> None:
        self.delegate.preflight()

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        completion = self.delegate.complete(messages, tools, timeout_s)
        key = request_key(self._spec, messages, tools)
        path = self.fixture_dir / f"{key.digest}.json"
        if path.exists():
            return completion

        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fixture_version": FIXTURE_VERSION,
            "key": key.to_dict(),
            "model": self._spec.to_dict(),
            "request": {
                "messages": [message.to_dict() for message in messages],
                "tools": [tool.to_dict() for tool in tools],
            },
            "response": completion.to_dict(),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        logger.info("Recorded fixture %s", path.name)
        return completion


ProviderFactory = Callable[..., Provider]

_PROVIDER_FACTORIES: Dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory. A live provider registers itself on import."""
    _PROVIDER_FACTORIES[name] = factory


def available_providers() -> List[str]:
    return sorted(_PROVIDER_FACTORIES)


def create_provider(name: str, spec: ModelSpec, **options: Any) -> Provider:
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        raise ProviderError(f"Unknown provider {name!r}; available: {available_providers()}")
    return factory(spec=spec, **options)


register_provider("replay", lambda spec, fixture_dir: ReplayProvider(fixture_dir=fixture_dir, spec=spec))

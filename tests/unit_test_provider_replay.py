import json
import socket
from pathlib import Path
from typing import List, Optional

import pytest

from scout_impl import provider as provider_module
from scout_impl.provider import (
    Completion,
    Message,
    ModelSpec,
    Provider,
    ProviderError,
    RecordingProvider,
    ReplayMiss,
    ReplayProvider,
    TokenUsage,
    ToolCall,
    ToolSpec,
    available_providers,
    create_provider,
    register_provider,
    request_key,
)

COMMITTED_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"

SYSTEM_PROMPT = (
    "You are SONiC Scout. Report only risks you can cite by file and line. "
    "Every claim needs a cause citation and an affected citation."
)
USER_PROMPT = (
    "Detector D2. The diff makes generate_golden_config_db.py read disabled_host_interfaces. "
    "Which ansible/vars/topo_*.yml declare host_interfaces without it?"
)

LIST_ARTIFACTS_TOOL = ToolSpec(
    name="list_artifacts",
    description="Enumerate a data-file family, e.g. every topology file.",
    parameters={"type": "object", "properties": {"family": {"type": "string"}}},
)


def _spec(model_id: str = "scout-test-model-2026-09-01") -> ModelSpec:
    return ModelSpec(
        provider="replay",
        model_id=model_id,
        input_usd_per_1k=0.003,
        output_usd_per_1k=0.015,
    )


def _messages() -> List[Message]:
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=USER_PROMPT),
    ]


class StubProvider(Provider):
    """Stands in for the live provider that this window deliberately does not implement."""

    def __init__(self, spec: ModelSpec, completion: Completion) -> None:
        super().__init__(spec)
        self.completion = completion
        self.requests: List[List[Message]] = []

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        self.requests.append(messages)
        return self.completion


def _stub_completion() -> Completion:
    return Completion(
        text="49 topology files declare host_interfaces without disabled_host_interfaces.",
        tool_calls=[ToolCall(id="call-1", name="list_artifacts", arguments={"family": "topology"})],
        usage=TokenUsage(input_tokens=1840, output_tokens=91),
        model_id="scout-test-model-2026-09-01",
    )


def test_request_key_is_stable_and_splits_prompt_from_input() -> None:
    first = request_key(_spec(), _messages(), [LIST_ARTIFACTS_TOOL])
    second = request_key(_spec(), _messages(), [LIST_ARTIFACTS_TOOL])

    assert first == second
    assert len(first.digest) == 64

    changed_input = list(_messages())
    changed_input[1] = Message(role="user", content="a different question")
    other = request_key(_spec(), changed_input, [LIST_ARTIFACTS_TOOL])
    assert other.prompt_sha == first.prompt_sha
    assert other.input_hash != first.input_hash
    assert other.digest != first.digest


def test_request_key_changes_with_prompt_model_and_tools() -> None:
    baseline = request_key(_spec(), _messages(), [LIST_ARTIFACTS_TOOL])

    changed_prompt = list(_messages())
    changed_prompt[0] = Message(role="system", content="a different system prompt")
    other_model = request_key(_spec("other-model-2026-01-01"), _messages(), [LIST_ARTIFACTS_TOOL])

    assert request_key(_spec(), changed_prompt, [LIST_ARTIFACTS_TOOL]).prompt_sha != baseline.prompt_sha
    assert other_model.digest != baseline.digest
    assert request_key(_spec(), _messages(), []).input_hash != baseline.input_hash


def test_temperature_and_token_budget_take_part_in_the_key() -> None:
    baseline = request_key(_spec(), _messages())
    warm = ModelSpec(provider="replay", model_id=_spec().model_id, temperature=0.7)

    assert request_key(warm, _messages()).digest != baseline.digest


def test_model_spec_defaults_to_deterministic_sampling() -> None:
    assert _spec().temperature == 0.0

    with pytest.raises(ValueError):
        ModelSpec(provider="replay", model_id="")


def test_recording_then_replaying_returns_the_same_completion(tmp_path: Path) -> None:
    spec = _spec()
    stub = StubProvider(spec, _stub_completion())
    recorder = RecordingProvider(stub, tmp_path)

    recorded = recorder.complete(_messages(), [LIST_ARTIFACTS_TOOL])
    replayed = ReplayProvider(tmp_path, spec).complete(_messages(), [LIST_ARTIFACTS_TOOL])

    assert replayed.text == recorded.text
    assert replayed.tool_calls == recorded.tool_calls
    assert replayed.usage == recorded.usage
    assert replayed.cached is True
    assert recorded.cached is False


def test_recorded_fixture_is_self_describing(tmp_path: Path) -> None:
    spec = _spec()
    RecordingProvider(StubProvider(spec, _stub_completion()), tmp_path).complete(_messages())

    fixtures = list(tmp_path.glob("*.json"))
    assert len(fixtures) == 1

    payload = json.loads(fixtures[0].read_text(encoding="utf-8"))
    assert fixtures[0].stem == payload["key"]["digest"]
    assert payload["model"]["model_id"] == spec.model_id
    assert payload["request"]["messages"][0]["role"] == "system"
    assert payload["response"]["usage"] == {"input_tokens": 1840, "output_tokens": 91}


def test_recording_does_not_rewrite_an_existing_fixture(tmp_path: Path) -> None:
    spec = _spec()
    stub = StubProvider(spec, _stub_completion())
    recorder = RecordingProvider(stub, tmp_path)

    recorder.complete(_messages())
    fixture = next(iter(tmp_path.glob("*.json")))
    fixture.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
    recorder.complete(_messages())

    assert json.loads(fixture.read_text(encoding="utf-8")) == {"sentinel": True}


def test_replay_miss_names_the_key_it_looked_for(tmp_path: Path) -> None:
    provider = ReplayProvider(tmp_path, _spec())

    with pytest.raises(ReplayMiss) as excinfo:
        provider.complete(_messages())

    expected = request_key(_spec(), _messages())
    assert expected.digest[:12] in str(excinfo.value)
    assert expected.prompt_sha[:12] in str(excinfo.value)


def test_provider_accumulates_token_usage_and_cost() -> None:
    provider = ReplayProvider(COMMITTED_FIXTURES, _spec())

    provider.complete(_messages(), [LIST_ARTIFACTS_TOOL])
    provider.complete(_messages(), [LIST_ARTIFACTS_TOOL])

    assert provider.call_count == 2
    assert provider.usage == TokenUsage(input_tokens=3680, output_tokens=182)
    assert provider.usd == pytest.approx(3680 * 0.003 / 1000 + 182 * 0.015 / 1000)


def test_committed_fixture_replays_without_a_key_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the replay provider must not open a socket")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    completion = ReplayProvider.from_fixtures(COMMITTED_FIXTURES).complete(
        _messages(), [LIST_ARTIFACTS_TOOL]
    )

    assert completion.cached is True
    assert completion.model_id == "scout-test-model-2026-09-01"
    assert "disabled_host_interfaces" in completion.text
    assert completion.tool_calls[0].name == "list_artifacts"


def test_from_fixtures_rejects_an_empty_or_mixed_directory(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        ReplayProvider.from_fixtures(tmp_path)

    RecordingProvider(StubProvider(_spec(), _stub_completion()), tmp_path).complete(_messages())
    other = ModelSpec(provider="replay", model_id="second-model-2026-01-01")
    RecordingProvider(StubProvider(other, _stub_completion()), tmp_path).complete(_messages())

    with pytest.raises(ProviderError):
        ReplayProvider.from_fixtures(tmp_path)


def test_registry_builds_a_provider_by_name(tmp_path: Path) -> None:
    provider = create_provider("replay", _spec(), fixture_dir=tmp_path)

    assert isinstance(provider, ReplayProvider)
    assert "replay" in available_providers()

    with pytest.raises(ProviderError):
        create_provider("not-registered", _spec())


def test_registering_a_new_provider_does_not_change_call_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider_module, "_PROVIDER_FACTORIES", dict(provider_module._PROVIDER_FACTORIES)
    )
    register_provider("stub", lambda spec: StubProvider(spec, _stub_completion()))

    provider = create_provider("stub", _spec())
    completion = provider.complete(_messages())

    assert completion.text.startswith("49 topology files")
    assert provider.usage.total_tokens == 1931


def test_empty_message_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        ReplayProvider(tmp_path, _spec()).complete([])


def test_token_usage_adds_and_totals() -> None:
    combined = TokenUsage(input_tokens=10, output_tokens=2) + TokenUsage(input_tokens=5, output_tokens=1)

    assert combined == TokenUsage(input_tokens=15, output_tokens=3)
    assert combined.total_tokens == 18

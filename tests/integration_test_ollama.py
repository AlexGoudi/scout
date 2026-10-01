"""Model-dependent proof that `OllamaProvider` talks to a real ollama, and what that costs.

**Not part of the offline suite.** The filename is outside the `unit_test_*.py` glob the
default run uses, and every test here additionally skips unless `$SCOUT_OLLAMA_TESTS=1`,
so `python3 -m pytest tests/unit_test_*.py -q` stays hermetic and model-free (NFR-10).

    SCOUT_OLLAMA_TESTS=1 python3 -m pytest tests/integration_test_ollama.py -q -s

`$SCOUT_OLLAMA_URL` names the server, defaulting here to `http://127.0.0.1:11435` rather
than ollama's own 11434, because on this machine the user-level server on 11435 is the one
with models. `$SCOUT_OLLAMA_MODEL` names the model, defaulting to `DEFAULT_MODEL` (`qwen2.5-coder:7b`).

What these prove that the stub cannot: that the wire shape is one a real server accepts,
that `format` really constrains the answer to the schema, that the token counts are real,
and that loopback bypasses this machine's corporate proxy. The one real call is capped at
64 output tokens, so it costs seconds, not minutes.
"""

import json
import os
import time

import pytest

from scout_impl.ollama import DEFAULT_MODEL, OLLAMA_URL_ENV, OllamaProvider, ollama_spec
from scout_impl.provider import RESPONSE_FORMAT, Message, ToolSpec

OLLAMA_TESTS_ENV = "SCOUT_OLLAMA_TESTS"
OLLAMA_MODEL_ENV = "SCOUT_OLLAMA_MODEL"
INTEGRATION_OLLAMA_URL = "http://127.0.0.1:11435"
MAX_OUTPUT_TOKENS = 64

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"covered": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["covered", "reason"],
}


@pytest.fixture
def provider() -> OllamaProvider:
    """A provider for the real server, only when these tests are opted in to."""
    if os.environ.get(OLLAMA_TESTS_ENV, "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip(
            f"ollama tests are opt-in: set ${OLLAMA_TESTS_ENV}=1 to call a real model "
            f"(${OLLAMA_URL_ENV} names the server, default {INTEGRATION_OLLAMA_URL}; "
            f"${OLLAMA_MODEL_ENV} the model, default {DEFAULT_MODEL})"
        )
    url = os.environ.get(OLLAMA_URL_ENV, "").strip() or INTEGRATION_OLLAMA_URL
    model = os.environ.get(OLLAMA_MODEL_ENV, "").strip() or DEFAULT_MODEL
    return OllamaProvider(ollama_spec(model, max_output_tokens=MAX_OUTPUT_TOKENS), base_url=url)


def test_preflight_finds_the_server_and_the_model(provider: OllamaProvider) -> None:
    started = time.monotonic()
    provider.preflight()

    print(f"\npreflight {provider.base_url} has {provider.spec.model_id}: {time.monotonic() - started:.3f}s")


def test_a_real_chat_call_answers_in_the_schema_and_reports_usage(provider: OllamaProvider) -> None:
    messages = [
        Message(role="system", content="Answer in JSON. Keep reason under ten words."),
        Message(role="user", content="Does a test named test_pfc_pause cover PFC pause frames?"),
    ]

    started = time.monotonic()
    completion = provider.complete(messages, [ToolSpec(name=RESPONSE_FORMAT, parameters=VERDICT_SCHEMA)])
    elapsed = time.monotonic() - started

    timings = provider.last_timings
    eval_s = timings.get("eval_duration") or 0.0
    prompt_s = timings.get("prompt_eval_duration") or 0.0
    output_rate = completion.usage.output_tokens / eval_s if eval_s else 0.0
    prompt_rate = completion.usage.input_tokens / prompt_s if prompt_s else 0.0
    print(f"\nchat    {completion.usage.input_tokens} in / {completion.usage.output_tokens} out tokens, "
          f"{elapsed:.2f}s wall, finish={completion.finish_reason}")
    print("timings " + ", ".join(f"{name} {seconds:.2f}s" for name, seconds in timings.items()))
    print(f"rates   prompt {prompt_rate:.0f} tok/s, output {output_rate:.1f} tok/s")
    print(f"answer  {completion.text}")

    assert completion.usage.input_tokens > 0
    assert completion.usage.output_tokens > 0
    assert provider.usd == 0.0
    answer = json.loads(completion.text)
    assert isinstance(answer, dict)
    assert set(VERDICT_SCHEMA["required"]) <= set(answer)

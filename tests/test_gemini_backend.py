"""Offline tests for the live Gemini backend: a fake SDK client, no network, no key, no cost.

The SDK-shape tests run only when google-genai is installed (the project venv); the
rest run on a bare standard-library interpreter.
"""

import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from infra import LLMClient, GeminiBackend, TransportError, LLMCallError, pricing_for, PRICES
from infra.pricing import estimate_run_cost

try:
    HAVE_SDK = importlib.util.find_spec("google.genai") is not None
except ModuleNotFoundError:  # no "google" namespace package at all
    HAVE_SDK = False
DECODING = {"temperature": 0.2, "top_p": 0.9, "seed": 7, "max_output_tokens": 64}
MESSAGES = [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"}, {"role": "user", "content": "Accept?"}]
ACCEPT = {"type": "object", "required": ["accept"], "properties": {"accept": {"type": "boolean"}}}


def fake_response(text="OK", prompt=12, output=5, thoughts=7, finish="STOP", model_version="gemini-x-001",
                  candidates=True, block=None):
    usage = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=output,
                            thoughts_token_count=thoughts, total_token_count=prompt + output + thoughts)
    candidate_list = [SimpleNamespace(finish_reason=SimpleNamespace(name=finish))] if candidates else []
    feedback = SimpleNamespace(block_reason=SimpleNamespace(name=block)) if block else None
    return SimpleNamespace(text=text, candidates=candidate_list, usage_metadata=usage,
                           model_version=model_version, response_id="r1", prompt_feedback=feedback)


class FakeClient:
    """Stands in for genai.Client: records generate_content kwargs and replays scripted outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class BuildRequestTests(unittest.TestCase):
    def test_messages_and_decoding_are_mapped(self):
        request = GeminiBackend.build_request("gemini-2.5-flash-lite", MESSAGES, DECODING, None, 32)
        self.assertEqual(request["model"], "gemini-2.5-flash-lite")
        self.assertEqual([c["role"] for c in request["contents"]], ["user", "model", "user"])
        self.assertEqual(request["contents"][1]["parts"], [{"text": "Hello"}])
        config = request["config"]
        self.assertEqual(config["system_instruction"], "Be terse.")
        self.assertEqual((config["temperature"], config["top_p"], config["seed"]), (0.2, 0.9, 7))
        self.assertEqual(config["max_output_tokens"], 32)
        self.assertNotIn("response_mime_type", config)
        self.assertNotIn("thinking_config", config)

    def test_schema_and_thinking_controls(self):
        request = GeminiBackend.build_request("m", MESSAGES, {**DECODING, "thinking_budget": 0}, ACCEPT, None)
        self.assertEqual(request["config"]["response_mime_type"], "application/json")
        self.assertEqual(request["config"]["response_json_schema"], ACCEPT)
        self.assertEqual(request["config"]["thinking_config"], {"thinking_budget": 0})
        request = GeminiBackend.build_request("m", MESSAGES, {**DECODING, "thinking_level": "minimal"}, None, None)
        self.assertEqual(request["config"]["thinking_config"], {"thinking_level": "MINIMAL"})

    def test_system_only_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            GeminiBackend.build_request("m", [{"role": "system", "content": "x"}], DECODING)

    @unittest.skipUnless(HAVE_SDK, "google-genai not installed")
    def test_request_dicts_validate_against_the_real_sdk_types(self):
        from google.genai import types
        request = GeminiBackend.build_request("gemini-2.5-flash-lite", MESSAGES,
                                              {**DECODING, "thinking_level": "low"}, ACCEPT, 32)
        config = types.GenerateContentConfig(**request["config"])
        self.assertEqual(config.thinking_config.thinking_level, types.ThinkingLevel.LOW)
        self.assertEqual(config.response_json_schema, ACCEPT)
        contents = [types.Content(**content) for content in request["contents"]]
        self.assertEqual(contents[1].role, "model")


class GenerateTests(unittest.TestCase):
    def test_usage_finish_reason_and_model_version_are_extracted(self):
        client = FakeClient([fake_response()])
        backend = GeminiBackend(client=client)
        output = backend.generate(model_id="m", messages=MESSAGES, decoding=DECODING, response_schema=None,
                                  max_output_tokens=16)
        self.assertEqual(output["text"], "OK")
        self.assertEqual(output["usage"], {"input_tokens": 12, "output_tokens": 5, "reasoning_tokens": 7})
        self.assertEqual(output["finish_reason"], "STOP")
        self.assertEqual(output["model_version"], "gemini-x-001")
        self.assertEqual(client.calls[0]["model"], "m")
        self.assertEqual(client.calls[0]["config"]["max_output_tokens"], 16)

    def test_blocked_prompt_gives_empty_text_and_block_reason(self):
        backend = GeminiBackend(client=FakeClient([fake_response(text=None, candidates=False, block="SAFETY")]))
        output = backend.generate(model_id="m", messages=MESSAGES, decoding=DECODING)
        self.assertEqual((output["text"], output["finish_reason"]), ("", "BLOCKED_SAFETY"))

    def test_missing_usage_counts_as_zero(self):
        response = SimpleNamespace(text="x", candidates=[], usage_metadata=None, model_version=None,
                                   response_id=None, prompt_feedback=None)
        output = GeminiBackend(client=FakeClient([response])).generate(model_id="m", messages=MESSAGES,
                                                                      decoding=DECODING)
        self.assertEqual(output["usage"]["input_tokens"], 0)
        self.assertIsNone(output["finish_reason"])

    def test_retryable_errors_become_transport_errors_and_others_propagate(self):
        for exc in (FakeAPIError(429), FakeAPIError(503), FakeAPIError(500), TimeoutError("slow"),
                    ConnectionError("down"), type("ServerError", (Exception,), {})("boom")):
            with self.subTest(exc=exc):
                backend = GeminiBackend(client=FakeClient([exc]))
                with self.assertRaises(TransportError):
                    backend.generate(model_id="m", messages=MESSAGES, decoding=DECODING)
        for exc in (FakeAPIError(400), FakeAPIError(401), FakeAPIError(404), ValueError("bad")):
            with self.subTest(exc=exc):
                backend = GeminiBackend(client=FakeClient([exc]))
                with self.assertRaises(type(exc)):
                    backend.generate(model_id="m", messages=MESSAGES, decoding=DECODING)

    @unittest.skipUnless(HAVE_SDK, "google-genai not installed")
    def test_real_sdk_error_classes_are_classified(self):
        import httpx
        from google.genai import errors
        self.assertTrue(GeminiBackend.is_transport_error(errors.ServerError(503, {"error": {"message": "x"}})))
        self.assertTrue(GeminiBackend.is_transport_error(errors.ClientError(429, {"error": {"message": "x"}})))
        self.assertFalse(GeminiBackend.is_transport_error(errors.ClientError(400, {"error": {"message": "x"}})))
        self.assertTrue(GeminiBackend.is_transport_error(httpx.ReadTimeout("t")))
        self.assertTrue(GeminiBackend.is_transport_error(httpx.ConnectError("c")))

    def test_missing_key_fails_before_any_import_or_network(self):
        with mock.patch.dict(os.environ, {"NO_SUCH_KEY_VAR": ""}, clear=False):
            os.environ.pop("NO_SUCH_KEY_VAR", None)
            with self.assertRaises(RuntimeError):
                GeminiBackend(api_key_env="NO_SUCH_KEY_VAR").client


class ThroughTheWrapperTests(unittest.TestCase):
    def test_measured_usage_cost_and_retry_through_llmclient(self):
        client = FakeClient([FakeAPIError(503), fake_response(text='{"accept": true}', prompt=100, output=10, thoughts=0)])
        sleeps = []
        llm = LLMClient(GeminiBackend(client=client), "gemini-2.5-flash-lite", DECODING,
                        pricing=pricing_for("gemini-2.5-flash-lite"), sleep=sleeps.append)
        record = llm.call(role="suspect", agent_id="suspect_1", purpose="acceptance",
                          phase="through_first_accusation", messages=MESSAGES, response_schema=ACCEPT)
        self.assertEqual(record["parsed"], {"accept": True})
        self.assertEqual([a["kind"] for a in record["attempts"]], ["initial", "transport_retry"])
        self.assertEqual(record["usage"]["input_tokens"], 100)
        self.assertAlmostEqual(record["cost_usd"], (100 * 0.10 + 10 * 0.40) / 1e6)
        self.assertEqual(record["model_version"], "gemini-x-001")
        self.assertEqual(sleeps, [1])
        self.assertEqual(len(client.calls), 2)

    def test_persistent_server_errors_surface_as_llmcallerror(self):
        client = FakeClient([FakeAPIError(500)] * 4)
        llm = LLMClient(GeminiBackend(client=client), "m", DECODING, sleep=lambda s: None)
        with self.assertRaises(LLMCallError) as context:
            llm.call(role="detective", purpose="question", phase="through_first_accusation", messages=MESSAGES)
        self.assertEqual(context.exception.record["status"], "transport_failure")
        self.assertTrue(all("HTTP 500" in a["error"] for a in context.exception.record["attempts"]))


class PricingTests(unittest.TestCase):
    def test_known_models_and_basis(self):
        for model_id, prices in PRICES.items():
            with self.subTest(model=model_id):
                pricing = pricing_for(model_id)
                self.assertEqual(pricing["input_per_million"], prices["input_per_million"])
                self.assertEqual(pricing["reasoning_per_million"], prices["output_per_million"])
                self.assertIn("checked", pricing["basis"])
        self.assertEqual(pricing_for("gemini-2.5-flash-lite")["output_per_million"], 0.40)

    def test_unknown_model_is_loud(self):
        with self.assertRaises(KeyError):
            pricing_for("mock-model")

    def test_run_cost_estimate(self):
        usage = {"input_tokens": 50_000, "output_tokens": 5_000, "reasoning_tokens": 0, "total_tokens": 55_000}
        cost = estimate_run_cost(usage, 300, pricing_for("gemini-2.5-flash-lite"))
        self.assertAlmostEqual(cost, 300 * (50_000 * 0.10 + 5_000 * 0.40) / 1e6)
        self.assertIsNone(estimate_run_cost(usage, 300, None))


if __name__ == "__main__":
    unittest.main()

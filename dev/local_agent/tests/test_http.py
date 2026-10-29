import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from rtl_agent.config import Config
from rtl_agent.errors import ModelError
from rtl_agent.llm import ChatClient, NoRedirect
from rtl_agent.chat_tools import TOOLS
from rtl_agent.context import request_estimate


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        # Most HTTP tests cover the OpenAI-compatible remote path. Local Qwen3
        # behavior is tested separately against Ollama's native /api/chat API.
        self.config = Config(base_url="https://api.deepseek.com/v1", model="deepseek-chat")

    def response(self, finish="stop"):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"files": []}',
                       "reasoning_content": "do not store this field"}, "finish_reason": finish}],
                       "usage": {"total_tokens": 123}, "model": "test"}).encode())

    def test_api_payload_and_secret_not_persisted(self):
        class Opener:
            def open(inner, request, **kwargs):
                self.assertEqual(request.full_url, "https://api.deepseek.com/v1/chat/completions")
                self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-not-real")
                self.assertEqual(json.loads(request.data)["response_format"], {"type": "json_object"})
                return self.response()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret-not-real"}), patch(
            "rtl_agent.llm.build_opener", return_value=Opener()
        ):
            ChatClient(self.config).complete([{"role": "user", "content": "test"}], self.output)
        all_files = "".join(p.read_text(encoding="utf-8") for p in self.output.iterdir())
        self.assertNotIn("test-secret-not-real", all_files)
        self.assertNotIn("do not store this field", all_files)

    def test_truncated_generation_stops(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener") as opener:
            opener.return_value.open.return_value = self.response("length")
            with self.assertRaisesRegex(ModelError, "截断"):
                ChatClient(self.config).complete([], self.output)

    def test_network_timeout_not_silently_retried(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("timeout")
            with self.assertRaises(ModelError):
                ChatClient(self.config).complete([], self.output)
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_rate_limit_retry_is_bounded(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener") as opener, patch("rtl_agent.llm.time.sleep"):
            opener.return_value.open.side_effect = HTTPError("https://example.invalid", 429, "rate limit", {}, None)
            with self.assertRaisesRegex(ModelError, "429"):
                ChatClient(self.config).complete([], self.output)
            self.assertEqual(opener.return_value.open.call_count, self.config.api_retries + 1)

    def test_baseline_does_not_force_json(self):
        class Opener:
            def open(inner, request, **kwargs):
                self.assertNotIn("response_format", json.loads(request.data))
                return self.response()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener", return_value=Opener()):
            ChatClient(self.config).complete([{"role": "user", "content": "test"}], self.output, json_mode=False)

    def test_adaptive_output_budget_uses_remaining_context(self):
        config = Config(context_window_tokens=10000, max_output_tokens=1,
                        context_safety_tokens=100, adaptive_output_tokens=True)
        client = ChatClient(config)
        short = client._payload([{"role": "user", "content": "short"}])
        long = client._payload([{"role": "user", "content": "x" * 1000}])
        self.assertGreater(short["max_tokens"], long["max_tokens"])
        self.assertEqual(short["max_tokens"], 10000 - request_estimate(
            [{"role": "user", "content": "short"}]) - 100)

    def test_retry_payload_reduces_output_budget_for_added_context(self):
        config = Config(context_window_tokens=10000, max_output_tokens=5000,
                        context_safety_tokens=100, adaptive_output_tokens=True)
        client = ChatClient(config)
        client.request_max_tokens = 5000
        first = client._payload([{"role": "user", "content": "short"}])
        retry = client._payload([{"role": "user", "content": "x" * 12000}])
        self.assertEqual(first["max_tokens"], 5000)
        self.assertLess(retry["max_tokens"], first["max_tokens"])

    def test_redirect_never_forwards_key(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_native_tools_request_and_null_content_response(self):
        class Opener:
            def open(inner, request, **kwargs):
                payload = json.loads(request.data)
                self.assertEqual(payload["tools"], TOOLS)
                self.assertEqual(payload["tool_choice"], "auto")
                self.assertNotIn("response_format", payload)
                return io.BytesIO(json.dumps({"choices": [{"message": {
                    "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "project_status", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}]}).encode())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener", return_value=Opener()):
            answer = ChatClient(self.config).converse([], TOOLS, self.output)
        self.assertEqual(answer["tool_calls"][0]["function"]["name"], "project_status")
        self.assertEqual(answer["content"], "")

    def test_ollama_qwen_native_tool_call_is_normalized(self):
        config = Config(base_url="http://127.0.0.1:11434/v1", model="qwen3:8b")
        raw = {"model": "qwen3:8b", "done": True, "done_reason": "stop", "message": {
            "role": "assistant", "content": "", "tool_calls": [{"id": "call_ollama",
                "function": {"name": "project_status", "arguments": {}}}]}}

        class Opener:
            def open(inner, request, **kwargs):
                self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
                payload = json.loads(request.data)
                self.assertFalse(payload["think"])
                self.assertNotIn("format", payload)
                self.assertNotIn("response_format", payload)
                self.assertEqual(payload["options"]["num_predict"], config.max_output_tokens)
                self.assertEqual(payload["tools"], TOOLS)
                return io.BytesIO(json.dumps(raw).encode())

        with patch("rtl_agent.llm.build_opener", return_value=Opener()):
            answer = ChatClient(config).converse(
                [{"role": "user", "content": "获取项目状态"}], TOOLS, self.output)
        call = answer["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "project_status")
        self.assertEqual(call["function"]["arguments"], "{}")

    def test_local_vllm_qwen_uses_openai_tools_and_template_settings(self):
        config = Config(base_url="http://127.0.0.1:8000/v1", model="Qwen3-8B",
                        context_window_tokens=32768, max_output_tokens=16384)
        client = ChatClient(config)
        payload = client._payload([{"role": "user", "content": "实现计数器"}], TOOLS)
        self.assertNotIn("think", payload)
        self.assertNotIn("/no_think", payload["messages"][-1]["content"])
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertFalse(config.uses_native_ollama())
        self.assertEqual(config.effective_context_tokens(), 32768)

        class Opener:
            def open(inner, request, **kwargs):
                self.assertEqual(request.full_url, "http://127.0.0.1:8000/v1/chat/completions")
                sent = json.loads(request.data)
                self.assertEqual(sent["tools"], TOOLS)
                self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
                return io.BytesIO(json.dumps({"choices": [{"message": {
                    "content": "", "tool_calls": [{"id": "vllm_call", "type": "function",
                        "function": {"name": "project_status", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}]}).encode())

        with patch("rtl_agent.llm.build_opener", return_value=Opener()):
            answer = client.converse(
                [{"role": "user", "content": "获取项目状态"}], TOOLS, self.output)
        self.assertEqual(answer["tool_calls"][0]["function"]["name"], "project_status")

    def test_qwen_empty_response_retry_preserves_thinking(self):
        config = Config(base_url="http://127.0.0.1:11434/v1", model="qwen3:8b",
                        ollama_think=True)
        client = ChatClient(config)
        payloads = []

        def respond(payload, output_dir):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"role": "assistant", "content": ""}
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "retry-call", "type": "function",
                "function": {"name": "project_status", "arguments": "{}"},
            }]}

        with patch.object(client, "_request", side_effect=respond):
            result = client.converse(
                [{"role": "user", "content": "获取项目状态"}], TOOLS, self.output)
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "project_status")
        self.assertTrue(payloads[0]["think"])
        self.assertIn("/think", payloads[0]["messages"][-1]["content"])
        self.assertTrue(payloads[1]["think"])
        self.assertIn("/think", payloads[1]["messages"][-1]["content"])

    def test_ollama_reasoning_stream_is_reported_but_not_added_to_message(self):
        config = Config(base_url="http://127.0.0.1:11434/v1", model="qwen3:8b",
                        ollama_think=True, ollama_show_reasoning=True)
        chunks = b"\n".join([
            json.dumps({"model": "qwen3:8b", "done": False,
                        "message": {"role": "assistant", "reasoning": "先分析工具。", "content": "实时"}}, ensure_ascii=False).encode(),
            json.dumps({"model": "qwen3:8b", "done": False,
                        "message": {"role": "assistant", "reasoning": "再提交参数。", "content": "回答"}}, ensure_ascii=False).encode(),
            json.dumps({"model": "qwen3:8b", "done": True, "done_reason": "stop",
                        "message": {"role": "assistant", "content": "", "tool_calls": [
                            {"function": {"name": "project_status", "arguments": {}}}]}}, ensure_ascii=False).encode(),
        ]) + b"\n"

        class Opener:
            def open(inner, request, **kwargs):
                payload = json.loads(request.data)
                self.assertTrue(payload["stream"])
                return io.BytesIO(chunks)

        seen = []
        content_seen = []
        client = ChatClient(config)
        client.reasoning_callback = seen.append
        client.content_callback = content_seen.append
        with patch("rtl_agent.llm.build_opener", return_value=Opener()):
            answer = client.converse(
                [{"role": "user", "content": "获取项目状态"}], TOOLS, self.output)
        self.assertEqual(seen, ["先分析工具。", "再提交参数。"])
        self.assertEqual(content_seen, ["实时", "回答"])
        self.assertEqual(client.last_reasoning, "先分析工具。再提交参数。")
        self.assertEqual(answer["content"], "实时回答")
        self.assertEqual(answer["tool_calls"][0]["function"]["name"], "project_status")
        self.assertEqual((self.output / "reasoning.txt").read_text(encoding="utf-8"), client.last_reasoning)

    def test_ollama_tool_history_uses_native_argument_objects(self):
        messages = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function", "function": {
                "name": "project_status", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"}]
        converted = ChatClient._ollama_messages(messages)
        self.assertNotIn("type", converted[0]["tool_calls"][0])
        self.assertEqual(converted[0]["tool_calls"][0]["function"]["arguments"], {})
        self.assertNotIn("tool_call_id", converted[1])

    def test_malformed_tool_call_is_rejected(self):
        for message in ({"content": "", "tool_calls": "invalid"},
                        {"content": "", "tool_calls": [{"type": "function", "function": None}]},
                        {"content": 0}):
            with self.subTest(message=message), patch.object(ChatClient, "_request", return_value=message):
                with self.assertRaises(ModelError):
                    ChatClient(self.config).converse([], TOOLS, self.output)

    def test_empty_model_response_is_resumable(self):
        client = ChatClient(self.config)
        with patch.object(ChatClient, "_request", return_value={"role": "assistant", "content": ""}):
            result = client.converse([], TOOLS, self.output)
        self.assertEqual(result, {"role": "assistant", "content": ""})
        self.assertTrue(client.last_diagnostics["empty_response"])

    def test_empty_response_retry_hint_matches_phase_tools(self):
        client = ChatClient(self.config)
        payloads = []

        def request(payload, output_dir):
            payloads.append(payload)
            return {"role": "assistant", "content": ""}

        rtl_tools = [next(item for item in TOOLS
                          if item["function"]["name"] == "write_design")]
        with patch.object(client, "_request", side_effect=request):
            client.converse([], rtl_tools, self.output)
        retry_content = payloads[1]["messages"][-1]["content"]
        self.assertIn("write_design", retry_content)
        self.assertNotIn("generate_testbench", retry_content)
        self.assertNotIn("必须调用一个可用的写入工具", retry_content)

    def test_non_object_message_raises_model_error(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake"}), patch("rtl_agent.llm.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"choices":[{"message":null}]}')
            with self.assertRaises(ModelError):
                ChatClient(self.config).converse([], TOOLS, self.output)


if __name__ == "__main__":
    unittest.main()

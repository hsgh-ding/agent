import json
import os
import time
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlparse

from .context import output_budget, request_estimate
from .errors import ModelError
from .storage import atomic_json, write_text


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an Authorization header to another endpoint.
        return None


class ChatClient:
    """DeepSeek or a local service exposing /chat/completions."""

    def __init__(self, config):
        self.config = config
        self.request_max_tokens = None
        # Set by the interactive chat loop.  It receives only newly generated
        # reasoning chunks, never the full conversation or tool arguments.
        self.reasoning_callback = None
        # Set by the interactive chat loop for terminal-only content streaming.
        # The complete content is still returned in the normal assistant message.
        self.content_callback = None
        self.last_reasoning = ""
        self.last_diagnostics = {}

    def complete(self, messages, output_dir, *, json_mode=True, schema=None):
        # Include the schema in budgeting; it is part of the actual request.
        payload = self._payload(messages, schema)
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "rtl_action", "schema": schema, "strict": True}}
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        message = self._request(payload, output_dir)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelError("模型返回空文本答案。请检查模型名称与接口配置。")
        return content

    def converse(self, messages, tools, output_dir):
        """Native function calling: the remote model chooses whether to use tools."""
        working_messages = list(messages)
        for attempt in range(2):
            attempt_dir = output_dir if attempt == 0 else output_dir / "retry_01"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            payload = self._payload(working_messages, tools)
            payload.update({"tools": tools, "tool_choice": "auto"})
            message = self._request(payload, attempt_dir)
            content = message.get("content")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise ModelError("模型 content 字段不是文本。")
            calls = message.get("tool_calls", [])
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                raise ModelError("模型 tool_calls 字段不是列表。")
            normalized, identifiers = [], set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ModelError("不支持的工具调用格式。")
                function = call.get("function", {})
                identifier = call.get("id")
                # Ollama native responses may omit the OpenAI call id.
                if not identifier and self._uses_native_ollama() and isinstance(function, dict):
                    identifier = "ollama_" + uuid4().hex[:12]
                if (not isinstance(identifier, str) or not identifier or identifier in identifiers or
                        not isinstance(function, dict) or not isinstance(function.get("name"), str) or
                        not isinstance(function.get("arguments"), str)):
                    raise ModelError("工具调用缺少有效 id/name/arguments。")
                identifiers.add(identifier)
                normalized.append({"id": identifier, "type": "function", "function": {
                    "name": function["name"], "arguments": function["arguments"]}})
            if content.strip() or normalized:
                assistant = {"role": "assistant", "content": content}
                if normalized:
                    assistant["tool_calls"] = normalized
                atomic_json(attempt_dir / "assistant.json", assistant)
                return assistant
            if attempt == 0:
                # Retry only a completed empty generation. Network errors and
                # length truncation are not retried, avoiding duplicate work.
                atomic_json(attempt_dir / "retry_meta.json", {
                    "reason": "empty assistant response", "retry_count": 1})
                available_names = [
                    item.get("function", {}).get("name") for item in tools
                    if isinstance(item, dict)
                ]
                available_names = [name for name in available_names if name]
                if available_names:
                    action_hint = (
                        "当前阶段必须执行动作。请保留必要的思考，但思考结束后必须输出一个工具调用；"
                        "不要以思考文字结束。当前可用工具只有：" + ", ".join(available_names) + "。"
                    )
                else:
                    action_hint = "当前没有可用工具；请直接返回最终文字，不要只输出思考。"
                working_messages = [*working_messages, {"role": "user", "content": (
                    "上一轮只有思考，没有最终文字或工具调用。" + action_hint
                )}]
        # An empty final response is not a transport failure. Keep the session
        # resumable without turning a local model's reasoning-only response
        # into an exception that looks like a completed action failure.
        self.last_diagnostics["empty_response"] = True
        empty = {"role": "assistant", "content": ""}
        atomic_json(output_dir / "assistant.json", empty)
        return empty

    def _payload(self, messages, tools=None):
        estimate = request_estimate(messages, tools)
        max_tokens = self.request_max_tokens
        available = output_budget(estimate, self.config)
        max_tokens = available if max_tokens is None else min(max_tokens, available)
        if max_tokens <= 0:
            raise ModelError("当前请求没有可用的输出预算；请减少上下文或增大 context_window_tokens。")
        payload = {"model": self.config.model, "messages": messages,
                "temperature": self.config.temperature, "max_tokens": max_tokens,
                "stream": False}
        if "qwen3" in self.config.model.lower():
            if self.config.uses_native_ollama():
                payload["think"] = self.config.ollama_think
                payload["messages"] = [dict(message) for message in messages]
                for message in reversed(payload["messages"]):
                    if message.get("role") == "user" and isinstance(message.get("content"), str):
                        mode = "/think" if self.config.ollama_think else "/no_think"
                        other_mode = "/no_think" if self.config.ollama_think else "/think"
                        message["content"] = message["content"].replace(other_mode, "").rstrip()
                        if mode not in message["content"]:
                            message["content"] += "\n" + mode
                        break
            else:
                payload["chat_template_kwargs"] = {
                    "enable_thinking": self.config.ollama_think}
        return payload

    def _uses_native_ollama(self):
        return ("qwen3" in self.config.model.lower()
                and self.config.uses_native_ollama())

    @staticmethod
    def _ollama_messages(messages):
        """Convert stored OpenAI tool history to Ollama's native chat shape."""
        converted = []
        tool_names = {}
        for message in messages:
            item = dict(message)
            if item.get("role") == "assistant" and item.get("tool_calls"):
                calls = []
                for call in item["tool_calls"]:
                    native_call = dict(call)
                    native_call.pop("type", None)
                    function = dict(native_call.get("function", {}))
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    function["arguments"] = arguments if isinstance(arguments, dict) else {}
                    native_call["function"] = function
                    if native_call.get("id") and function.get("name"):
                        tool_names[native_call["id"]] = function["name"]
                    calls.append(native_call)
                item["tool_calls"] = calls
            elif item.get("role") == "tool":
                call_id = item.pop("tool_call_id", None)
                tool_name = item.get("tool_name") or tool_names.get(call_id)
                item.pop("name", None)
                if tool_name:
                    item["tool_name"] = tool_name
            converted.append(item)
        return converted

    def _request(self, payload, output_dir):
        config = self.config
        self.last_reasoning = ""
        self.last_diagnostics = {}
        key = os.environ.get(config.api_key_env, "")
        if not key and urlparse(config.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ModelError(f"环境变量 {config.api_key_env} 未设置。请在本机设置，别把密钥写入配置或报告。")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        native_ollama = ("qwen3" in config.model.lower()
                         and config.uses_native_ollama())
        if native_ollama:
            native_payload = dict(payload)
            response_format = native_payload.pop("response_format", None)
            if response_format:
                native_payload["format"] = (response_format["json_schema"]["schema"]
                                            if response_format.get("type") == "json_schema" else "json")
            native_payload.pop("tool_choice", None)
            native_payload["think"] = False
            native_payload["options"] = {
                "num_predict": native_payload.pop("max_tokens"),
                "num_ctx": config.ollama_context_tokens,
                "temperature": config.temperature,
            }
            native_payload.pop("temperature", None)
            native_payload["messages"] = self._ollama_messages(native_payload["messages"])
            # Streaming lets the terminal show progress while a local model is
            # thinking.  It is enabled only for the explicit diagnostic mode.
            native_payload["stream"] = bool(
                self.content_callback is not None or
                (config.ollama_show_reasoning and config.ollama_think)
            )
            endpoint = "http://" + (urlparse(config.base_url).netloc or "127.0.0.1:11434") + "/api/chat"
            if "think" in payload:
                native_payload["think"] = bool(payload["think"])
            request = Request(endpoint, data=json.dumps(native_payload).encode("utf-8"),
                              headers=headers, method="POST")
            logged_payload = native_payload
        else:
            request = Request(config.base_url.rstrip("/") + "/chat/completions",
                              data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            logged_payload = payload
        # Log the exact JSON sent to the API, after adapting it for Ollama.
        atomic_json(output_dir / "request.json", logged_payload)
        opener = build_opener(NoRedirect())
        for retry in range(config.api_retries + 1):
            try:
                with opener.open(request, timeout=config.api_timeout_seconds) as response:
                    if native_ollama and native_payload.get("stream"):
                        data, message = self._read_ollama_stream(response, output_dir)
                    else:
                        data = json.loads(response.read().decode("utf-8"))
                        message = None
                # Reasoning is diagnostic-only and is never returned as assistant
                # content, so it cannot enter the next tool-call context.
                if native_ollama:
                    message = message or data["message"]
                    choice = {"message": message, "finish_reason": data.get("done_reason")}
                    for call in message.get("tool_calls", []):
                        # Ollama 原生接口省略 OpenAI 兼容格式中的 type 字段；
                        # 补齐后再交给下面统一的 tool_call 校验逻辑。
                        call["type"] = "function"
                        function = call.get("function", {})
                        if isinstance(function.get("arguments"), dict):
                            function["arguments"] = json.dumps(function["arguments"], ensure_ascii=False)
                else:
                    choice = data["choices"][0]
                    message = choice["message"]
                if not isinstance(message, dict):
                    raise ModelError("模型返回的 message 不是对象。")
                if not self.last_reasoning and config.ollama_show_reasoning:
                    for field in ("reasoning", "thinking", "reasoning_content"):
                        value = message.get(field)
                        if isinstance(value, str) and value:
                            self.last_reasoning = value
                            break
                content = message.get("content")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    raise ModelError("模型返回的 content 不是文本。")
                write_text(output_dir / "answer.txt", content)
                if self.last_reasoning:
                    write_text(output_dir / "reasoning.txt", self.last_reasoning)
                usage = data.get("usage")
                timings = None
                if native_ollama:
                    usage = {"prompt_tokens": data.get("prompt_eval_count"),
                             "completion_tokens": data.get("eval_count")}
                    timings = {key: data.get(key) for key in (
                        "load_duration", "prompt_eval_duration", "eval_duration", "total_duration")
                        if data.get(key) is not None}
                atomic_json(output_dir / "response_meta.json", {
                    "id": data.get("id"), "model": data.get("model"),
                    "usage": usage, "timings": timings,
                    "finish_reason": choice.get("finish_reason")})
                if choice.get("finish_reason") == "length":
                    raise ModelError("模型输出达到 max_output_tokens，未把截断代码交给 Vivado。请增加输出预算后重试。")
                return message
            except HTTPError as exc:
                # Some APIs echo inputs/credentials in their error bodies. Don't persist them.
                if (exc.code == 429 or 500 <= exc.code < 600) and retry < config.api_retries:
                    time.sleep(min(2 ** retry, 8))
                    continue
                raise ModelError(f"模型接口 HTTP {exc.code}。检查密钥、余额、模型名、接口地址与上下文预算；"
                                 "原始服务端错误体未写入日志。") from exc
            except (URLError, TimeoutError, OSError) as exc:
                # A network timeout may already have consumed tokens; don't silently repeat it.
                raise ModelError("模型网络请求失败或超时，已保存现场。可恢复运行；重发可能产生额外 API 费用。") from exc
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ModelError("模型接口返回的结构不符合 chat/completions 格式。") from exc

    def _read_ollama_stream(self, response, output_dir):
        """Read Ollama NDJSON, exposing reasoning without changing tool history."""
        data = {}
        message = {"role": "assistant", "content": ""}
        tool_calls = None
        reasoning_parts = []
        for raw_line in response:
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict):
                continue
            data.update({key: value for key, value in chunk.items() if key != "message"})
            part = chunk.get("message") or {}
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("role"), str):
                message["role"] = part["role"]
            content = part.get("content")
            if isinstance(content, str):
                message["content"] += content
                callback = self.content_callback
                if callback is not None and content:
                    callback(content)
            for field in ("reasoning", "thinking", "reasoning_content"):
                piece = part.get(field)
                if isinstance(piece, str) and piece:
                    reasoning_parts.append(piece)
                    callback = self.reasoning_callback
                    if callback is not None:
                        callback(piece)
                    break
            if isinstance(part.get("tool_calls"), list) and part["tool_calls"]:
                tool_calls = part["tool_calls"]
        if tool_calls:
            message["tool_calls"] = tool_calls
        self.last_reasoning = "".join(reasoning_parts)
        self.last_diagnostics = {"streamed": True, "reasoning_chars": len(self.last_reasoning)}
        return data, message


class DemoClient:
    """Offline demonstration, explicitly NOT an actual model evaluation."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, output_dir, *, json_mode=True):
        self.calls += 1
        from .demo import COUNTER_RTL
        content = COUNTER_RTL if self.calls > 1 else COUNTER_RTL.replace("count <= count + 8'd1;", "count <= count + ;")
        response = json.dumps({"plan": "离线演示：首轮故意产生语法错误，第二轮修复。",
                               "files": [{"path": "rtl/top.sv", "content": content}]}, ensure_ascii=False)
        atomic_json(output_dir / "request.json", {"demo": True, "messages": messages})
        write_text(output_dir / "answer.txt", response)
        return response

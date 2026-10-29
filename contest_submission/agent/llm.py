"""LLM backend.

Two backends, one code path:

    LLM_BACKEND=mock      no network, no key. Returns a canned stub derived from
                          the interface. Use this to verify the run.sh contract
                          and the judging pipeline without a GPU or an API key.

    LLM_BACKEND=openai    any OpenAI-compatible /chat/completions endpoint.
                          This covers BOTH a commercial API bought by the team
                          (development only) AND a local vLLM server (what the
                          submission must actually use).

    development   LLM_BASE_URL=https://api.deepseek.com/v1
    local model   LLM_BASE_URL=http://localhost:8000/v1

Only the environment variables change; the agent does not.

NOTE FOR SUBMISSION: the final run happens in an offline sandbox. MODEL.md must
declare locally deployable open weights. A commercial API fails conditions 2.1
and 2.3 of the scoring rules and produces no run-based score at all.
"""

from __future__ import annotations

import json
import os
import re
import textwrap


class LLMError(RuntimeError):
    pass


class LLMResult:
    def __init__(self, text: str, tokens_in: int = 0, tokens_out: int = 0):
        self.text = text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


def _load_generation() -> dict:
    """The thinking settings declared in manifest.json ("generation").

    baseline.py reads the same block and sends it with its request, so the plan
    and the baseline run the model in the same mode. The rules require the agent
    to use the declared settings; this is the reference way to do it. Same
    allowlist as baseline.py: chat_template_kwargs with scalar values, and
    reasoning_effort.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            gen = json.load(f).get("generation") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out: dict = {}
    kw = gen.get("chat_template_kwargs")
    if isinstance(kw, dict):
        kw = {k: v for k, v in kw.items()
              if isinstance(k, str) and isinstance(v, (bool, int, float, str))}
        if kw:
            out["chat_template_kwargs"] = kw
    effort = gen.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        out["reasoning_effort"] = effort
    return out


class LLM:
    def __init__(self) -> None:
        self.backend = os.environ.get("LLM_BACKEND", "mock").strip().lower()
        self.model = os.environ.get("LLM_MODEL", "mock-model")
        self.base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        # 16384 与基线一致：推理模型先输出思考，上限过小会在写出代码前被截断
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
        self.timeout_s = float(os.environ.get("LLM_TIMEOUT_S", "600"))
        self.generation = _load_generation()

        if self.backend == "openai" and not self.base_url:
            raise LLMError("LLM_BACKEND=openai requires LLM_BASE_URL")

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "base_url": self.base_url or None,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "generation": self.generation,
        }

    def chat(self, messages: list[dict]) -> LLMResult:
        if self.backend == "mock":
            return self._chat_mock(messages)
        if self.backend == "openai":
            return self._chat_openai(messages)
        raise LLMError(f"unknown LLM_BACKEND: {self.backend!r}")

    # ---------------------------------------------------------------- openai

    def _chat_openai(self, messages: list[dict]) -> LLMResult:
        import requests  # imported lazily so mock mode needs no dependency

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
            # 与基线相同的思考设置，来自 manifest.json，见 _load_generation()
            **self.generation,
        }

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        body = resp.json()
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:500]}") from exc

        usage = body.get("usage") or {}
        return LLMResult(
            text=text,
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
        )

    # ------------------------------------------------------------------ mock

    def _chat_mock(self, messages: list[dict]) -> LLMResult:
        """Return something shaped like a real answer, without a model.

        The point is to exercise the plumbing -- prompt assembly, code
        extraction, tool invocation, trace writing, the HTTP shell -- not to
        produce a correct module. The stub declares the right ports and drives
        every output to zero: it elaborates, so the pipeline runs end to end,
        and it fails simulation, which is honest.
        """
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        header, outputs = _parse_module(user)
        if header is None:
            # 评测题集不下发 interface.txt，题面里也没有 module 声明，端口清单
            # 是 `- input clk` 这样的列表。桩模式必须走和评测当天同一条路，
            # 否则它验证的链路和真正要跑的那条不是一回事。
            header, outputs = _header_from_prompt(user)

        if header:
            body = header + "\n"
            body += "  // MOCK BACKEND -- placeholder body, not a real solution.\n"
            body += "  // Set LLM_BACKEND=openai to get a generated implementation.\n"
            for name, width in outputs:
                lit = "1'b0" if width <= 1 else f"{width}'b0"
                body += f"  assign {name} = {lit};\n"
            body += "endmodule\n"
        else:
            body = ("// MOCK BACKEND: 既没有 module 声明，也没能从题面解析出端口清单\n")

        text = "以下是实现：\n\n```verilog\n" + body + "```\n"
        return LLMResult(text=text, tokens_in=len(user) // 4, tokens_out=len(text) // 4)


# --------------------------------------------------------------------- utils

_MODULE_RE = re.compile(r"module\s+(\w+)\s*(?:#\s*\([^)]*\)\s*)?\((.*?)\)\s*;", re.DOTALL)


def _header_from_prompt(text: str) -> tuple[str | None, list[tuple[str, int]]]:
    """从题面的端口清单拼出 module 头。

    复用 tools.ports_from_prompt，与 check_interface 用的是同一份解析，
    免得两处对「题面里的端口表」有两套理解。
    """
    from .tools import ports_from_prompt

    name, ports = ports_from_prompt(text)
    if not name or not ports:
        return None, []
    decls = []
    for direction, width, pname in ports:
        w = "" if width <= 1 else f" [{width - 1}:0]"
        decls.append(f"  {direction}{w} {pname}")
    header = f"module {name} (\n" + ",\n".join(decls) + "\n);"
    outputs = [(n, w) for d, w, n in ports if d == "output"]
    return header, outputs


def _parse_module(text: str) -> tuple[str | None, list[tuple[str, int]]]:
    """Return the module header verbatim plus its output ports and widths."""
    m = _MODULE_RE.search(text)
    if not m:
        return None, []

    header = m.group(0)
    outputs: list[tuple[str, int]] = []
    for decl in m.group(2).split(","):
        decl = decl.strip()
        if not decl.lower().startswith("output"):
            continue
        rng = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", decl)
        width = abs(int(rng.group(1)) - int(rng.group(2))) + 1 if rng else 1
        name = decl.split()[-1].strip("[]() \t")
        if name:
            outputs.append((name, width))
    return header, outputs


def strip_think(text: str) -> str:
    """去掉推理模型的思考段，只留正式回答。

    推理服务未配置 reasoning parser 时，Qwen3 等模型会把 <think>…</think> 原样
    放在正文里。不先去掉，思考过程会连同代码一起写进产物，编译必然失败；思考中
    打过的代码草稿也可能被当成答案。三种形态：
      <think>…</think>答案     完整的思考段
      …</think>答案            对话模板已预填 <think>，回复里只有结束标记
      <think>…（截断）         输出上限耗尽在思考中，没有答案
    """
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        return text.split("<think>", 1)[0]
    return text


def extract_code(text: str, preferred_top: str | None = None) -> str:
    """Pull the Verilog out of a model reply.

    Models wrap code in fences with wildly inconsistent labels, and small models
    sometimes emit prose after the closing fence, or no fence at all. Handle the
    three cases in decreasing order of confidence.
    """
    text = strip_think(text)
    blocks = re.findall(
        r"```(?:systemverilog|verilog|sv|v|)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        # A model may emit an interface echo, an RTL candidate, and a testbench
        # in one answer. Length alone is unsafe: a testbench is often the
        # longest block. Prefer the requested top, one-module candidates, and
        # blocks without simulation-only constructs; use length only as a final
        # tie-breaker.
        def rank(block: str) -> tuple[int, int, int, int]:
            modules = re.findall(
                r"\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)\b", block,
                re.IGNORECASE,
            )
            top_hit = int(bool(preferred_top and preferred_top in modules))
            one_module = int(len(modules) == 1 and "endmodule" in block)
            simulation_free = int(not re.search(
                r"\binitial\b|\$(?:finish|fatal|display|monitor)\b|\bprogram\b|\binterface\b",
                block, re.IGNORECASE,
            ))
            return top_hit, one_module, simulation_free, len(block)

        return max(blocks, key=rank).strip() + "\n"

    # 无代码围栏时，若整体形似代码则全部取用。
    if "module" in text and "endmodule" in text:
        return text.strip() + "\n"

    return ""

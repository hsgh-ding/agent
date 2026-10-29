from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

from .errors import AgentError


@dataclass
class Config:
    # Only the variable name is saved; the secret is read at request time.
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3:8b"
    vivado: str = "vivado"
    run_root: str = "runs"
    top: str = "top"
    tb_top: str = "tb_top"
    part: str = "xczu3eg-sbva484-1-e"
    clocks: list = field(default_factory=lambda: [{"port": "clk", "period_ns": 5.0}])
    simulation_ns: int = 10000
    max_attempts: int = 4
    max_chat_steps: int = 16
    repeated_error_limit: int = 3
    stage_timeout_seconds: int = 900
    # 本地 qwen3:8b 在 CPU/GPU 混合运行时约 7-8 token/s；完整 RTL、
    # 测试台和工具参数可能需要数分钟才能生成完。
    api_timeout_seconds: int = 600
    api_retries: int = 2
    # User-configured budget, NOT an assertion about the provider's model limit.
    context_window_tokens: int = 16384
    # Large RTL should use chunked writes so one generation does not consume
    # nearly the entire local context window.
    max_output_tokens: int = 4096
    context_safety_tokens: int = 2048
    adaptive_output_tokens: bool = False
    # Ollama model context size; explicitly sent as num_ctx for local requests.
    ollama_context_tokens: int = 16384
    # Thinking can help complex planning but costs latency and generation budget.
    ollama_think: bool = False
    # Print Ollama/Qwen reasoning chunks to the terminal and save them per request.
    # This is diagnostic output only; reasoning is never added to the chat history.
    ollama_show_reasoning: bool = False
    # Legacy config keys remain readable; chat always uses model tool calling.
    auto_verify: bool = False
    execution_mode: str = "tools"
    temperature: float = 0.1

    def uses_native_ollama(self):
        parsed = urlparse(self.base_url)
        return (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.port == 11434)

    def effective_context_tokens(self):
        """Return the smaller of the app budget and the Ollama runtime window."""
        limit = self.context_window_tokens
        if self.uses_native_ollama() and "qwen3" in self.model.lower():
            limit = min(limit, self.ollama_context_tokens)
        return limit

    def validate(self):
        for name in ("top", "tb_top", "api_key_env"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", getattr(self, name)
            ):
                raise AgentError(f"配置 {name} 必须是普通标识符。")
        if self.top == self.tb_top:
            raise AgentError("top 和 tb_top 不能同名。")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.part):
            raise AgentError("part 不是有效的器件字符串。")
        for name in ("simulation_ns", "max_attempts", "max_chat_steps", "repeated_error_limit",
                     "stage_timeout_seconds", "api_timeout_seconds",
                     "context_window_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or (name != "max_output_tokens" and value == 0):
                raise AgentError(f"配置 {name} 必须是正整数；max_output_tokens 可为 0。")
        for name in ("api_retries", "context_safety_tokens", "ollama_context_tokens"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise AgentError(f"配置 {name} 必须是非负整数。")
        if self.ollama_context_tokens == 0:
            raise AgentError("ollama_context_tokens 必须是正整数。")
        effective_context = self.effective_context_tokens()
        if self.context_safety_tokens >= effective_context:
            raise AgentError("上下文窗口必须大于安全余量。")
        if self.max_output_tokens and self.max_output_tokens + self.context_safety_tokens >= effective_context:
            raise AgentError("固定输出预算与安全余量不能占满上下文窗口。")
        if type(self.adaptive_output_tokens) is not bool:
            raise AgentError("adaptive_output_tokens 必须是布尔值。")
        if type(self.ollama_think) is not bool:
            raise AgentError("ollama_think 必须是布尔值。")
        if type(self.ollama_show_reasoning) is not bool:
            raise AgentError("ollama_show_reasoning 必须是布尔值。")
        if type(self.auto_verify) is not bool:
            raise AgentError("auto_verify 必须是布尔值。")
        if self.execution_mode not in {"tools", "structured"}:
            raise AgentError("execution_mode 必须是 tools 或 structured。")
        if not self.adaptive_output_tokens and self.max_output_tokens == 0:
            raise AgentError("关闭自适应输出时，max_output_tokens 必须为正整数。")
        if not isinstance(self.temperature, (float, int)) or not 0 <= self.temperature <= 2:
            raise AgentError("temperature 必须介于 0 和 2。")
        if not isinstance(self.clocks, list):
            raise AgentError("clocks 必须是列表。")
        seen = set()
        for clock in self.clocks:
            if not isinstance(clock, dict) or set(clock) != {"port", "period_ns"}:
                raise AgentError("每个时钟必须包含 port 和 period_ns。")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(clock["port"])):
                raise AgentError("时钟端口必须是普通标识符。")
            period = clock["period_ns"]
            if not isinstance(period, (float, int)) or not math.isfinite(period) or period <= 0:
                raise AgentError("period_ns 必须是有限正数。")
            if clock["port"] in seen:
                raise AgentError("时钟端口不能重复。")
            seen.add(clock["port"])
        parsed = urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
            raise AgentError("远程模型接口须使用 HTTPS；本机服务允许 HTTP。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AgentError("base_url 不允许包含密钥、用户名、查询参数或 fragment。")
        if not self.model or not self.vivado or not isinstance(self.run_root, str) or not self.run_root:
            raise AgentError("model、vivado 和 run_root 不能为空。")
        self.auto_verify = False
        self.execution_mode = "tools"
        return self

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            return cls(**data).validate()
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError(f"无法读取配置 {path}: {exc}") from exc

    def to_dict(self):
        return asdict(self)

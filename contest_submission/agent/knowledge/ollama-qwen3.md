# Ollama Qwen3 chat and tool calling

关键词：本地模型、原生工具调用、函数调用、空回复、模型思考、上下文长度。

- Ollama's native chat endpoint is POST /api/chat. For local Ollama, the
  OpenAI-compatible /v1/chat/completions URL is not the native tool-call
  protocol; the agent adapts Qwen3 requests to /api/chat.
- Native requests use options.num_ctx for the runtime context window,
  options.num_predict for the maximum generated tokens, and think to enable
  or disable Qwen3 thinking. These are separate settings.
- For local diagnosis, start the chat with `--show-thinking`. The agent then
  streams Qwen3's `reasoning`/`thinking` field to the terminal and saves it as
  `model/<call>/reasoning.txt`; it is deliberately excluded from chat history.
- Tool calls are returned in message.tool_calls. A tool result should be
  sent back as a message with role: "tool" and the matching tool_name,
  followed by another assistant request. Ollama's native tool history uses
  argument objects, while the OpenAI-compatible format commonly uses JSON text.
- Keep tool schemas small and purpose-specific. Use one tool per turn, validate
  arguments in Python, preserve the tool result, and let the model decide the
  next action only after seeing that result.
- A completed empty assistant response may be retried once with a short
  corrective prompt. Do not silently retry network timeouts or truncated
  generations because the original request may already have consumed time or
  tokens.

References:

- https://docs.ollama.com/capabilities/tool-calling
- https://docs.ollama.com/api/chat

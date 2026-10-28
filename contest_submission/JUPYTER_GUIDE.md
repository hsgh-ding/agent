# Radeon Cloud Jupyter 使用

在官方 RTL 赛道提供的 Radeon Cloud Notebook 中操作。建议先运行官方
`notebooks/rtl_quickstart.ipynb` 的环境检查、判定器检查和 mock 步骤，再克隆你的私有方案仓库。
模型权重放在 `/workspace` 持久盘，EDA 临时文件放在 `/var/tmp`；不要把模型上传到 Git，
也不要把大批 Vivado 临时文件放在 4 GB 的 `/tmp`。

## 下载并固定模型

以下代码取当前公开仓库的 commit SHA，并用这个 SHA 下载，之后复现时权重不会随 `main` 改变：

```python
from huggingface_hub import HfApi, snapshot_download

repo_id = "Qwen/Qwen3-8B"
revision = HfApi().model_info(repo_id, revision="main").sha
model_dir = "/workspace/models/Qwen3-8B"
snapshot_download(repo_id=repo_id, revision=revision, local_dir=model_dir)
print("revision:", revision)
print("model directory:", model_dir)
```

把 SHA 写入 `manifest.json` 的 `model.revision` 和 `model/MODEL.md`。如果 Hugging Face 访问失败，
先保留 Notebook 的下载错误输出；不要改用无法公开复现的临时链接。

## 启动模型与验证方案

假设私有仓库克隆在 `/workspace/<队伍仓库>`，官方仓库在 `/workspace/rtlagent2026`：

```bash
TEAM=/workspace/<队伍仓库>
OFFICIAL=/workspace/rtlagent2026
TASK="$OFFICIAL/tasks/ex01_popcount8"
cd "$TEAM"

MODEL=/workspace/models/Qwen3-8B ./serve/start.sh
python -m pip show vllm
rocm-smi --showmemuse
```

记录 vLLM 版本和模型完成一次请求后的显存用量，再填入 `manifest.json` 的 `serving.version`、
`model.measured_vram_gb` 及 `model/MODEL.md`。显存上限按赛事规则是单卡 32 GB；`serve/vllm.sh`
默认以 30 GB 预算启动。若实测超过上限，应先降低预算或模型配置并重测。

先验证方案的输入输出链路，再用真实模型运行一题：

```bash
LLM_BACKEND=mock ./run.sh "$TASK" /tmp/agent-mock
LLM_BACKEND=openai LLM_BASE_URL=http://127.0.0.1:8000/v1 \
  LLM_API_KEY=dummy LLM_MODEL=Qwen3-8B LLM_MAX_TOKENS=16384 \
  ./run.sh "$TASK" /tmp/agent-qwen
```

用官方判定器看等级。再对同一道题运行基线，保证方案和基线连接同一个模型服务：

```bash
cd "$OFFICIAL/selftest"
python3 judge.py --task "$TASK" --solution /tmp/agent-qwen/solution.v
cd "$TEAM"
LLM_BACKEND=openai LLM_BASE_URL=http://127.0.0.1:8000/v1 \
  LLM_API_KEY=dummy LLM_MODEL=Qwen3-8B LLM_MAX_TOKENS=16384 \
  ./run_baseline.sh "$TASK" /tmp/agent-baseline
```

打开 `/tmp/agent-qwen/trace.jsonl` 查看生成、接口检查、lint、综合及各轮耗时。mock 输出是空壳，
只验证脚本链路，不代表 Agent 解题正确。正式测试还应跑官方完整自测集，并按相同上下文、
思考模式和模型配置比较方案与基线。

## 启动投题 API

赛事投题 API 与 `/v1/chat/completions` 模型 API 是不同服务。vLLM 在本机回环地址 8000；
方案服务在回环地址 7860。只通过 Radeon Cloud 隧道暴露 7860。

```bash
cd "$TEAM"
export FPGACHINA_TOKEN='<赛事方发给你的令牌>'
export MODEL_NAME=Qwen3-8B
./serve/run_api.sh start
curl -H "Authorization: Bearer $FPGACHINA_TOKEN" \
  http://127.0.0.1:7860/v1/health
```

健康检查应返回 `"track":"rtl"`。`POST /v1/solve` 接收 `task_id`、`prompt`、可选的
`interface`、`mode` 和 `deadline_s`，立即返回 `job_id`；用
`GET /v1/jobs/<job_id>` 轮询，完成后返回 `solution`、`trace` 和 `elapsed_s`。接口请求样例、
令牌要求和隧道命令见 [`serve/README.md`](serve/README.md)。令牌只设为环境变量，不要写入仓库。

测试完成后可用 `./serve/run_api.sh stop` 停止投题服务。停止 vLLM 可在 Notebook 清理格运行：

```bash
pkill -f vllm.entrypoints.openai.api_server
```

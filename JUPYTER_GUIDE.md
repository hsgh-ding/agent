# 在 Radeon Cloud Jupyter 测试比赛 Agent

下面命令在 JupyterLab 的 **Terminal** 里运行，不是在本地 Windows 终端。流程是：先测脚本链路，再启动模型写一道题，最后用官方判定器给 RTL 打 L1/L2/L3。

## 已配置好 Qwen3-8B 后，复测本次修复

先把本地最新提交包的**内容**提交到 GitHub 仓库根目录并推送，或将新的源代码 ZIP 解压覆盖到
`/workspace/agent/`。本地修改不会自动同步到 Jupyter。服务已经启动时执行：

```bash
git config --global --add safe.directory /workspace/agent
cd /workspace/agent
git pull --ff-only && OFFICIAL=/workspace/rtlagent2026 bash ./tools/retest_jupyter.sh
```

脚本确认当前提交根目录、非零数量的包内测试及声明的技能文件完整，再检查官方 `env.sh`
没覆盖模型或 AGENT_DIR/OUT_DIR 配置，并确认 8000 端口返回 `Qwen3-8B`，
再调用官方自测，Agent 和 baseline 一起跑。
新结果写入单独的 `/var/tmp/rtl-compare-日期_时间/`，保留旧结果用于比较。若模型服务没启动，
先按第 4 节启动；脚本不会改用 mock。若提示 `env.sh overrides`，打开
`/workspace/rtlagent2026/selftest/env.sh`，将 `LLM_BACKEND` 设为 `openai`、`LLM_MODEL` 设为
`Qwen3-8B`、`LLM_BASE_URL` 设为 `http://127.0.0.1:8000/v1`，再重跑。

看汇总里的 **Agent pass@1 与 baseline pass@1**，以及每题是否从 L1 到 L3；只看到包内测试
通过不能判断分数提高。每题的 `trace.jsonl` 应出现一次 `request_behavior_review`，默认复核的
`spec_review` 记录题面/源码引用与具体检查。`match_claim`/`mismatch_claim` 都是模型判断，
不是仿真结论；后者触发 `request_semantic_repair`，修改后重新检查。功能正确性仍以官方 judge 为准。
非法报告最多纠正一次；`invalid_report_original_retained` 表示没有拿到有效报告，保留原代码。
新版支持局部修改，可在 trace 中查看 `edit_validation`。`verification_cache`
表示相同源码的检查结果被复用，实际工具调用不重复计数。

比较旧自查与新报告时，分别用 `AGENT_REVIEW_MODE=legacy bash tools/retest_jupyter.sh` 和
`AGENT_REVIEW_MODE=evidence bash tools/retest_jupyter.sh` 跑同一题集；用
`AGENT_BEHAVIOR_REVIEW=0 bash tools/retest_jupyter.sh` 可关闭复核。保持模型配置一致，比较
各次官方成绩、退化题数和耗时；三道题的一次结果只能作初步检查，不能证明普遍提升。

## 1. 确认仓库目录

你的 GitHub 仓库是 `hsgh-ding/agent`，提交入口直接在仓库根目录。若还没克隆：

```bash
cd /workspace
git clone https://github.com/hsgh-ding/agent.git
```

官方仓库如果尚未放在 `/workspace/rtlagent2026`，再克隆：

```bash
git clone https://gitee.com/Vickyiii/rtlagent2026.git /workspace/rtlagent2026
```

设置路径并确认关键文件存在：

```bash
REPO=/workspace/agent
TEAM="$REPO"
OFFICIAL=/workspace/rtlagent2026
TASK="$OFFICIAL/tasks/ex03_lfsr8"
test -f "$TEAM/run.sh" && test -f "$TEAM/manifest.json" && test -f "$TASK/prompt.txt" && echo "路径正确"
```

如果 Windows 的改动还没推送到 GitHub，Jupyter 克隆到的仍是旧代码。将最新提交包内容上传到仓库根目录；仓库已克隆时这样更新：

```bash
git config --global --add safe.directory /workspace/agent
cd /workspace/agent
git pull --ff-only && python3 tools/check_submission.py
TEAM=/workspace/agent
```

这一步只跑包内单元测试，不需要 GPU 或模型。真实模型 A/B 测试请等第 4–5 节启动 Qwen 服务并
完成环境设置后，再按第 7 节的命令执行；否则脚本可能会使用 mock。

## 2. 检查磁盘、Vivado 和模型文件

```bash
df -h /workspace /var/tmp
command -v xvlog xelab xsim vivado
ls -lh /workspace/models/Qwen3-8B/model-*.safetensors
```

若 Vivado 命令没找到，先尝试加载平台的环境：`source /tools/Xilinx/2026.1/Vivado/settings64.sh`，再查一次。检查模型索引列出的分片是否都存在：

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path("/workspace/models/Qwen3-8B")
index = json.loads((p / "model.safetensors.index.json").read_text())
missing = sorted(set(index["weight_map"].values()) - {f.name for f in p.glob("*.safetensors")})
print("missing shards:", missing)
if missing:
    raise SystemExit("模型仍未下载完整，先续传")
print("模型分片完整")
PY
```

模型目录需要有完整权重分片和 `model.safetensors.index.json`。如果权重没下载完，先续传，不要启动 vLLM。模型权重放 `/workspace`；EDA 临时文件放 `/var/tmp`，不要放 `/tmp` 或把大日志写进快满的目录。设置临时目录和 Vivado 授权，供 Agent 与官方判定器共同使用：

设置比赛运行环境：

```bash
export OMP_NUM_THREADS=16
export AGENT_TMP=/var/tmp
export EDA_TMP=/var/tmp/eda
export XILINXD_LICENSE_FILE="${XILINXD_LICENSE_FILE:-2100@vivado-lic.fpgachina.svc.cluster.local}"
export FLEXLM_TIMEOUT="${FLEXLM_TIMEOUT:-300000000}"
mkdir -p "$AGENT_TMP" "$EDA_TMP"
```

## 3. 先用 mock 检查 Agent 文件链路

这一步不调用模型，只验证 Agent 能读题、写 `solution.v` 和 `trace.jsonl`：

```bash
cd "$TEAM"
LLM_BACKEND=mock ./run.sh "$TASK" /var/tmp/rtl-mock
ls -lh /var/tmp/rtl-mock
cat /var/tmp/rtl-mock/solution.v
cat /var/tmp/rtl-mock/trace.jsonl
```

mock 代码是占位输出，**不能用它判断设计功能，也不能当比赛分数**。

先用任务内的参考 RTL 验证官方判定器和 Vivado 授权。若参考设计不能到 L3，先修判定环境，不要把环境故障当成 Agent 的设计错误：

```bash
cd "$OFFICIAL/selftest"
python3 judge.py --task "$TASK" --solution "$TASK/reference/solution.sv"
```

## 4. 启动 Qwen3-8B 服务

确认模型完整后运行：

```bash
cd "$TEAM"
MODEL=/workspace/models/Qwen3-8B MAX_LEN=32768 VRAM_BUDGET_GB=30 ./serve/start.sh
```

`start.sh` 会在后台启动 vLLM，并等到模型 API 就绪。用下面命令确认：

```bash
curl -fsS http://127.0.0.1:8000/v1/models
python3 -m pip show vllm
rocm-smi --showmemuse
```

提交规则按单卡 32 GB 显存计算；即使 W7900 显示 48 GB，也不要把 `VRAM_BUDGET_GB` 调到 32 以上。

## 5. 让 Agent 真正解一道题并用官方判分

```bash
cd "$TEAM"
LLM_BACKEND=openai \
LLM_BASE_URL=http://127.0.0.1:8000/v1 \
LLM_API_KEY=dummy \
LLM_MODEL=Qwen3-8B \
LLM_MAX_TOKENS=16384 \
AGENT_TMP=/var/tmp \
./run.sh "$TASK" /var/tmp/rtl-qwen
```

查看 Agent 生成的 DUT 和调用记录：

```bash
cat /var/tmp/rtl-qwen/solution.v
cat /var/tmp/rtl-qwen/trace.jsonl
```

然后让**官方判定器**运行隐藏参考测试台，检查编译、功能仿真和综合：

```bash
cd "$OFFICIAL/selftest"
python3 judge.py --task "$TASK" --solution /var/tmp/rtl-qwen/solution.v
```

判定结果的 L1 是可编译，L2 是官方仿真功能通过，L3 是综合通过。比赛 Agent 不生成测试台；L2 使用的是官方提供给判定器的参考测试台。

## 6. 与官方基线对照

Agent 和基线必须使用同一个 vLLM 服务、同一模型和 `manifest.json` 中声明的推理设置。先运行基线：

```bash
cd "$TEAM"
LLM_BASE_URL=http://127.0.0.1:8000/v1 \
MODEL_NAME=Qwen3-8B \
./run_baseline.sh "$TASK" /var/tmp/rtl-baseline
```

再用相同判定器评分：

```bash
cd "$OFFICIAL/selftest"
python3 judge.py --task "$TASK" --solution /var/tmp/rtl-baseline/solution.v
```

一题只能检查流程是否通畅。要判断 Agent 是否比基线好，需要对同一批题、同一模型配置同时跑 Agent 和基线，再比较官方 `score.json`。不要把 mock 分数当成真实成绩。

## 7. 小批量题集测试

确认第 4–5 节的模型服务已启动且 Agent/baseline 环境变量已设置后，先用官方现有题集做 A/B：

```bash
cd "$OFFICIAL/selftest"
LLM_BACKEND=openai \
LLM_BASE_URL=http://127.0.0.1:8000/v1 \
LLM_API_KEY=dummy \
LLM_MODEL=Qwen3-8B \
LLM_MAX_TOKENS=16384 \
MODEL_NAME=Qwen3-8B \
./run_selftest.sh \
  --tasks "$OFFICIAL/tasks" \
  --agent "$TEAM" \
  --out /var/tmp/rtl-ross-repair \
  --samples 1
```

查看终端末尾的 Agent/baseline pass@1 与归一化增益，并检查
`/var/tmp/rtl-ross-repair/score.json`。单元测试通过不代表真实模型输出正确，必须用这一步比较
整体题集分及逐题等级是否超过 baseline。模型复核仍是静态推理，最终功能以官方判定器为准。

如果某题只用了 2–3 秒就得到 L0，先看 Agent 现场而不是继续调提示词：

```bash
find /var/tmp/rtl-ross-repair -name trace.jsonl -o -name solution.v | sort

# 把下面两个路径替换成对应题目的实际文件
cat /var/tmp/rtl-ross-repair/agent/ex01_popcount8/s0/run.log
cat /var/tmp/rtl-ross-repair/agent/ex01_popcount8/s0/trace.jsonl
wc -c /var/tmp/rtl-ross-repair/agent/ex01_popcount8/s0/solution.v
cat /var/tmp/rtl-ross-repair/agent/ex01_popcount8/s0/solution.v
```

重点看 `trace.jsonl`：`llm rc=1` 表示模型请求失败，`extract rc=1` 表示模型没有可抽取
代码，`candidate_rejected` 表示接口或源代码预检拒绝，`lint rc=1` 才是进入 Vivado 后的
编译/展开失败。不要只看最终 L0，因为这四类原因的修复方向完全不同。

确认单题链路正常后，再用 VerilogEval v2 的 20 道题做开发测试，先不要直接跑 156 道 × 5 次：

```bash
cd /workspace
git clone https://github.com/NVlabs/verilog-eval.git
cd "$OFFICIAL/selftest"
python3 veval_import.py \
  --src /workspace/verilog-eval/dataset_spec-to-rtl \
  --out /var/tmp/tasks_veval20 \
  --limit 20
./run_selftest.sh \
  --tasks /var/tmp/tasks_veval20 \
  --agent "$TEAM" \
  --out /var/tmp/rtl-eval20 \
  --samples 1
```

这个脚本会跑方案和基线、调用官方判定器，并汇总 `score.json`。临时题集、Vivado 日志和中间结果留在 `/var/tmp`；测完只把汇总分数复制到持久盘：

```bash
cp /var/tmp/rtl-eval20/score.json /workspace/rtl-eval20-score.json
```

确认磁盘足够后再扩大题集或采样次数；跑完整题集会花很久并产生大量日志。VerilogEval 只适合开发期对照，不等于官方隐藏题集成绩。

## 8. 测完后

查看批测汇总：

```bash
cat /workspace/rtl-eval20-score.json
```

停止 vLLM 时：

```bash
pkill -f vllm.entrypoints.openai.api_server
```

投题 HTTP API（7860 端口）是正式服务接口测试；如果你目前只想测 Agent 解题，不需要启动 `serve/run_api.sh` 或暴露隧道端口。

## 9. 判断 Agent 是否变好

必须让 Agent 和官方 `baseline` 用同一个 Jupyter 实例里的 Qwen3-8B 服务、同一题集、同一输出
上限、思考设置和采样次数。`baseline` 是裸模型一次生成；Agent 的分数高于 baseline 才能说明
当前 Agent 流程有增益。只看 Agent 自己的分数不能判断 RAG、技能或重试有没有帮助。

官方每题只取最高级：L0=0，L1=0.2，L2=0.7，L3=1.0。`pass@1` 是计入能力/增益的主指标；
`pass@5` 只是稳定性诊断。比如 Agent `0.667`、baseline `0.733`，表示本次 Agent 比裸模型低，
不是优化成功。题数少时单题影响很大，最好先跑完整官方自测集，再对有变化的版本重复多次。

官方归一化增益为 `g = (agent_score - baseline_score) / (1 - baseline_score)`；当 baseline 已是
1.0 时分母为零，该轮不能计算增益。增益项得分为 `40 × min(g / 0.6, 1)`，负增益按 0 分计。
同时查看失败题的 trace：接口/抽取失败优先修格式，lint 失败看 Vivado 首错，L1 但未到 L2
说明功能语义错，L2 未到 L3 才是综合问题。不要仅凭模型“说成功”判断，官方 judge 的等级才是准数。

建议的比较流程：先只跑一次确认链路，再至少用 3 道以上不同题检查趋势；每次只改变一项
（例如一条 skill 或重试轮数），并记录 `score.json`、各题级别、耗时和失败类别。改动后的
结果低于 baseline，就先回看失败题和 `trace.jsonl`，不要继续叠提示词。

## 10. 投题服务与 rc-tunnel 检查

投题令牌和模型下载令牌不是一回事。`FPGACHINA_TOKEN` 是赛事方发给队伍的业务鉴权令牌，
`serve/serve_api.py` 用它保护 `/v1/health`、`/v1/solve` 和 `/v1/jobs/...`；它既不是
Hugging Face token，也不是 LLM API key。不要把它写入 Git 或提交包。

在 Jupyter 实例终端执行：

```bash
source /workspace/env.sh 2>/dev/null || true
cd /workspace/agent
export FPGACHINA_TOKEN='赛事方令牌'
export MODEL_NAME=Qwen3-8B
export MODEL=/workspace/models/Qwen3-8B
export AGENT_TMP=/var/tmp

./serve/start.sh
./serve/run_api.sh start
curl -i -H "Authorization: Bearer $FPGACHINA_TOKEN" \
  http://127.0.0.1:7860/v1/health
```

本机健康检查必须先返回 HTTP 200 和 `"ready":true`。随后检查隧道所连接的服务器：

```bash
grep -E '^[[:space:]]*serverAddr' /root/.local/state/rc-tunnel/frpc.toml
"$HOME/.local/bin/rc-tunnel" status
```

看到 `serverAddr = rc-tun-cn.anruicloud.com` 且状态 `ready: true` 表示隧道控制面正常。
如果仍是 `frp-tunnel.amd-cloud.com`、公网地址返回 404，先停止旧隧道，再在没有代理变量的
环境中重新暴露 7860，并保持该终端进程运行：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  "$HOME/.local/bin/rc-tunnel" expose --port 7860
```

`status` 输出公网 URL 后，在自己的电脑（不是 Jupyter 实例）检查：

```powershell
$env:FPGACHINA_TOKEN = "赛事方令牌"
curl.exe -i -H "Authorization: Bearer $env:FPGACHINA_TOKEN" `
  "https://公网地址/v1/health"
```

结果含义：`200 + ready:true` 表示可投题；`401` 是令牌错误；`500` 是实例中的
`FPGACHINA_TOKEN` 没设置；`404` 通常是隧道转发到错误端口或旧隧道；连接超时则先检查
`serve/run_api.sh status`、本机 7860 健康检查和 `rc-tunnel status`。

# RTL track submission package

此目录是 RTL 赛道的提交包。正式运行入口、异步投题接口和模型启动脚本遵循官方参考仓库；
模型元数据尚需在 Radeon Cloud 上完成实测，见 [`model/MODEL.md`](model/MODEL.md) 和
[`JUPYTER_GUIDE.md`](JUPYTER_GUIDE.md)。

这是一个可运行的 RTL 赛道方案包，适合作为 `<队伍编号>_rtlagent/` 仓库内容。它加入了
接口契约和错误分类技能、离线知识检索、候选版本保留、RTL 静态预检，以及针对明确 Vivado
`VRFC 10-1280` 输出声明错误的窄范围确定性修复；该自动修复通过 lint/综合后，再让模型做
一次受限的逐拍语义复核。**这些实现改动尚未在
Radeon Cloud 官方完整题集上测分；是否提高分数要以同模型、同配置的方案/基线对照为准。**

---

## 一、这个目录里有什么

标「**要改**」的是竞赛需要队伍投入的部分，标「不改」的照原样带上即可。

| 文件 / 目录 | 作用 | 是否需要改 |
| --- | --- | --- |
| `run.sh` | 单题入口。赛事方投一道题，就调它一次 | 不改 |
| `run_baseline.sh` | 基线入口 | **不得修改** |
| `baseline.py` | 赛事方提供的基线脚本，增益项以它的结果为基线 | **不得修改** |
| `agent/main.py` | 智能体主流程：几轮循环、时间预算、写 `trace.jsonl` | **要改** |
| `agent/tools.py` | 接口/RTL hygiene 检查、窄范围 10-1280 修复、lint、综合 | **要改** |
| `agent/llm.py` | 调用大模型的封装 | 一般不改 |
| `agent/skills.py` | 技能包的加载与挑选 | 视需要 |
| `agent/prompts/system.md` | 主提示词，告诉模型该怎么写 RTL | **要改** |
| `agent/prompts/repair.md` | 报错后让模型改错用的提示词 | **要改** |
| `skill/` | 接口、复位、FSM/位宽和 Vivado 诊断技能 | **要改** |
| `agent/knowledge/competition/` | 离线 Verilog、时序、位宽、接口和 Vivado 知识 | **要改** |
| `model/MODEL.md` | 模型声明。不如实填写，增益项 40 分不得分 | **必须填** |
| `serve/vllm.sh` | 启动本地推理服务 | 按所选模型改 |
| `serve/start.sh` | 拉起全部常驻进程，就绪后返回。两种评测方式都从这里起服务 | 有自己的常驻进程时要改 |
| `serve/serve_api.py` | 赛事方投题用的 HTTP 接口，监听 `127.0.0.1:7860` | 一般不改 |
| `serve/run_api.sh` | 后台启动投题接口，退出后自动重启 | 一般不改 |
| `REPORT.md` | 设计报告 | **必须写** |
| `Dockerfile` | 基于官方基础镜像构建 | 按需要改 |
| `manifest.json` | 机器可读的方案声明 | **必须填** |

一句话概括：**改 `agent/`、`skill/`、`prompts/`，填 `MODEL.md` 与 `REPORT.md`，
其余照原样带上。`baseline.py` 与 `run_baseline.sh` 绝对不要动。**

---

## 二、怎么跑起来

三步，建议按顺序做。第一步不需要 GPU，也不需要装 Vivado。

### 第 1 步：桩模式，先确认链路是通的

```bash
pip install -r requirements.txt
LLM_BACKEND=mock ./run.sh ../tasks/ex03_lfsr8 /tmp/out
cat /tmp/out/solution.v
```

`mock` 是假模型，不联网、不要 key。它返回一个端口正确、输出恒为 0 的空壳模块。
这一步只验证「读题目 → 写文件」这条路通不通，不验证解题能力。

看到 `/tmp/out/` 下生成了 `solution.v` 和 `trace.jsonl`，就算成功。

### 第 2 步：接上真模型，看看能不能解题

开发阶段可以用自己买的商用 API：

```bash
export LLM_BACKEND=openai
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=sk-xxxxxxxx
export LLM_MODEL=deepseek-coder
./run.sh ../tasks/ex03_lfsr8 /tmp/out
```

任何 OpenAI 兼容的端点都可以。**这条路径只能用于开发调试，不能用于提交。**

### 第 3 步：换成本地模型（正式提交必须是这个）

```bash
MODEL=/models/Qwen3-8B ./serve/vllm.sh &     # 先起本地推理服务

export LLM_BACKEND=openai
export LLM_BASE_URL=http://localhost:8000/v1
export LLM_API_KEY=dummy
export LLM_MODEL=Qwen3-8B
./run.sh ../tasks/ex03_lfsr8 /tmp/out
```

第 2 步和第 3 步的区别只有环境变量，智能体代码一行都不用改。

### 另外：跑一次基线

```bash
./run_baseline.sh ../tasks/ex03_lfsr8 /tmp/base
```

基线用同一个推理服务、同一个模型，但绕过智能体与技能包，单次直接生成。
增益项 40 分以它的结果为基线，必须能跑通，否则这 40 分不得分。

---

## 三、智能体用的检查工具

`agent/tools.py` 里有四个检查工具，智能体靠它们发现自己写错了。按代价从小到大：

| 工具 | 需要 Vivado | 耗时 | 能发现什么 |
| --- | --- | --- | --- |
| `check_interface()` | 否 | 微秒 | 模块名、端口名、方向、位宽与题目要求不符 |
| `check_source_hygiene()` | 否 | 微秒 | RTL 中误写的 `initial`、延时和测试台系统任务 |
| `lint()` | 是 | 数秒 | 语法错误、模块内部的未定义引用 |
| `synth()` | 是 | 数十秒 | 不可综合的写法、意外产生的锁存器与多驱动 |

先查接口再 lint 再综合，是因为**接口写错的代价最大**：端口名或模块名写错，
参考测试台就例化不了这个模块，整题直接 0 分；而功能写错至少还能拿 L1 的 0.2 分。
接口对不对用纯文本比较就能判定，没必要花一次 `lint()` 去发现它。

`check_interface()` 不调模型也不调工具，纯文本比较。这顺带说明一件事：
**技能不必涉及大模型**，一段能机械判定的检查同样计分。

未安装 Vivado 时，`lint()` 与 `synth()` 会返回 `rc=-1` 并说明原因，智能体退化为
「生成 + 接口检查」两步，不会在没真正检查的情况下报告检查通过。

---

## 四、环境变量

常用的几个：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_BACKEND` | `mock` | 填 `mock` 或 `openai` |
| `LLM_BASE_URL` | 无 | 模型服务地址，用 `openai` 后端时必填 |
| `LLM_API_KEY` | 空 | 本地 vLLM 可填任意值 |
| `LLM_MODEL` | `mock-model` | 模型名 |
| `AGENT_MAX_ROUNDS` | `4` | 最多重试几轮 |

其余用于微调行为，一般不必改动：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_MAX_TOKENS` | `16384` | 单次输出上限，须与 `MODEL.md` 一致 |
| `LLM_TEMPERATURE` | `0.2` | 采样温度 |
| `LLM_TIMEOUT_S` | `600` | 单次模型调用超时 |
| `AGENT_DEADLINE_S` | `900` | 单题时间预算，评测时由投题参数覆盖 |
| `AGENT_RESERVE_S` | `20` | 留给写出结果的余量 |
| `AGENT_TMP` | `/tmp` | 临时目录。实例上设为 `/var/tmp`，不要用 `/workspace` |
| `RTL_PART` | `xczu3eg-sbva484-1-e` | 综合器件 |
| `RTL_TOP` | 从题面推断，默认 `TopModule` | 顶层模块名 |
| `FPGACHINA_TOKEN` | 无 | 投题令牌，`serve_api.py` 必填 |

---

## 五、如何判断优化是否真的提高分数

不要用“提示词更长”“技能更多”当作成绩证据。先固定验证集，再在完全相同的模型 revision、
量化、上下文、思考设置和超时下分别跑方案与官方 baseline；记录逐题 L0/L1/L2/L3、耗时、
失败类别和 `trace.jsonl`。之后一次只改一个因素（例如 RAG、某条 skill 或重试轮数），重复完整
测试，观察通过率是否稳定提升。自测集结果不等于官方隐藏集成绩。

官方测试台不会交给 Agent，所以当前 lint/synth 成功仍无法证明功能正确。不要向 RTL 里塞入
自测台，也不要把模型自测当官方验证。当前最重要的下一项工作是先在 Radeon Cloud 跑通官方
题集和 baseline，定位错误主要来自接口、功能、综合还是超时，再按数据补知识条目。

提交包的技能和知识库均离线加载，没有 embedding 模型或在线检索服务；仅注入最多两条匹配内容，
避免挤占 RTL 输出上下文。多 Agent/MCP 尚未加入：对当前单卡和时限，额外模型轮次及驻留进程
需要先证明能提高题集分数，才值得纳入正式方案。

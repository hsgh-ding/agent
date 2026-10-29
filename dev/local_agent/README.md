# RTL Agent：用 Ollama/Qwen3 对话并驱动 Vivado

这套代码是一个可持续对话的 RTL Agent。你先和本机 Ollama 的 Qwen3 讨论需求；当你明确要求实现、修改或验证时，模型通过函数调用选择受控工具，Python 写出 RTL，再由 Vivado Tcl 创建工程、编译、仿真和综合。失败时完整保留日志并交给模型修复。每条用户消息默认最多修改 4 次、请求模型 16 次，达到上限后等待你继续对话。
## Knowledge base, chunked code, and model-invoked verification tools

The repository `knowledge/` directory is an offline, editable knowledge base. The `skills/` directory contains guidance for using the verification tools. They include Verilog coding, testbench, Vivado diagnostics, Ollama tool calling, context budgeting, and verification workflow topics. The model retrieves short relevant excerpts through `search_knowledge`; the full knowledge base is never placed in the context. Add Markdown topics without a vector database or network service.

For large designs, use `write_design_chunk` to save each `rtl/` file in pieces under the session `draft/` directory, then call `commit_design` to create an immutable revision. Later turns use `project_status` for the file index and `read_source` for selected line ranges. The complete RTL remains on disk and is not repeated in every model request.

Prefer `run_verification_workflow` for validation. With no user-supplied testbench it runs `compile/elaboration -> synthesis`; with a trusted `--testbench` it runs `compile -> simulation -> synthesis`. It stops at the first failure and preserves logs. `read_report` and `read_source` support paged reads so large reports and source files do not consume the model context.

**当前终点是综合网表，不做布局布线、比特流或下载板卡。** 使用配置中的 Vivado、器件 `xczu3eg-sbva484-1-e`、时钟 5ns。

## 先看这三个路径

| 用途 | 默认位置 |
|---|---|
| Vivado 启动器 | `config.example.json` 中的 `vivado` 路径 |
| Agent 源码 | `F:/codex_task/fpga/rtl_agent` |
| 新工程、日志、仿真和工具临时目录 | 配置中的 `run_root`，默认 `F:/rtl_agent_runs/<时间戳>` |

**不用改 Windows 中文用户名，不用重装 Vivado。** 安装在哪个盘和工程保存在哪个盘是两个设置。运行目录必须为英文路径，Vivado/XSim 在本机中文路径下实测出现乱码和 `xsim.ini` 写入失败。代码会提前检查，避免浪费 API 请求。`APPDATA`、`TEMP`、`TMP` 只为 Vivado 子进程设置到新运行目录，系统设置不变；`XILINX_LOCAL_USER_DATA=no` 避免本次批处理安装用户 Tcl 插件。

每个运行使用一个新子目录，不能指向已有工程。若你无法写入默认目录，在 `config.example.json` 改 `run_root`，例如 `D:/rtl_agent_runs`。

## 最容易开始的方式

Radeon Cloud 远程 Jupyter、模型选择和赛道打包差距见 [CLOUD_JUPYTER_GUIDE.md](CLOUD_JUPYTER_GUIDE.md)。赛道官方上手 Notebook 是 `notebooks/rtl_quickstart.ipynb`，本仓库的 CLI Agent 尚不能直接替换官方方案包。

在 VS Code 打开 `F:/codex_task/fpga/rtl_agent` 文件夹，然后打开终端（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

启动脚本会找 Python，然后出现 `你>` 对话提示。本机 Ollama 不需要 API 密钥；远程 HTTPS 服务才需要配置对应的环境变量。它不会自动运行计数器，也不会用演示答案代替真实 API。密钥只放当前进程环境，不写入源码、配置或日志。

排查 Qwen3 长时间不返回或没有调用工具时，可以打开思考诊断：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -ShowThinking
# 或：python main.py chat --show-thinking
```

这会启用 Qwen3 的 `/think` 模式，终端实时显示 `[思考] ...` 片段；每个模型请求目录还会保存
`reasoning.txt`。思考文本只用于诊断，不会加入下一轮上下文，因此不会增加后续请求的上下文长度。
思考模式会明显变慢、消耗更多输出预算；确认问题后去掉 `--show-thinking` 即可恢复默认的 `/no_think`。

例如：

```text
你> 我想做一个带同步低电平复位的 8 位计数器，先告诉我接口和验证方案
AI> ...（讨论，不调用工具）
你> 方案可以，开始生成代码并用 Vivado 编译、仿真、综合
[工具] write_design
[工具] run_verification_workflow
...
```

你可以继续追问或要求修改；修复时 Python 会把关键报错和小型 RTL 一起放进同一轮模型输入，模型直接修改原文件。大文件才按需读取。输入 `/status` 查看状态，`/paste` 粘贴多行需求，`/new gcd` 或 `/new counter` 创建独立任务，`/tasks` 列出任务，`/switch gcd` 切换任务，`/quit` 退出。会话目录会打印在启动时，重新运行可用 `--session-dir` 恢复。

所有电路使用同一套真实工具调用协议：模型通过 `write_design` 或分块写入工具提交 DUT RTL。
模型没有测试台生成工具，也不能在 RTL 参数中提交测试台。写入成功且用户要求完成流程时，Python 自动调用
`run_verification_workflow`：没有测试台就执行 RTL 编译/展开和综合；只有用户用 `--testbench` 提供可信测试台时才执行功能仿真。
成功后由 Python 调用 `finish_design` 导出。不再根据计数器、加法器、GCD 等名字选择模板，不会自动生成特定电路的测试台或替换模型代码。
`/verify` 仍是用户主动执行当前设计验证的快捷命令。

聊天会话按阶段限制工具，防止模型在写 RTL 阶段反复重写：

```text
design    -> write_design / write_design_chunk / commit_design
verify    -> Python 自动执行 run_verification_workflow
repair    -> Python 附带错误和小型 RTL -> edit_source 原地修复
finish    -> finish_design
```

模型生成 RTL 后，写入工具会从下一轮工具列表中移除。验证失败时，Vivado 报告节选会固定在下一轮上下文中，
模型可以用 `read_report` 读取完整日志，修改 RTL 后重新进入验证阶段。

当前配置为 `execution_mode: "tools"`。`auto_verify` 只作为旧配置兼容字段保留；当用户要求完成实现/综合时，
写入 RTL 后由 Python 固定执行验证。旧配置中的结构化模式会迁移为工具模式，不再切回已删除的结构化控制器。旧会话的源码、报告和冻结测试台保留；
要使用不同接口或测试台请通过 `/new` 新建任务。重新启动 Agent 后加载新版代码。

复现普通入口的真实模型与 Vivado 测试（运行目录必须不存在）：

```powershell
python tests/ollama_smoke.py --scenario generated-counter4 --work-dir F:/rtl_agent_runs/my_counter4_check --repeat 2 --vivado
```

如果脚本找不到 Python，请安装 Python 3.10+，或指定已安装的解释器：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -PythonExe 'D:\Python312\python.exe'
```

无需安装 LangChain、requests 或其他 Python 包，本项目只用标准库。Ollama 请求默认只访问本机；离线 demo 不调用 API。

## 手动使用

以下 `python` 指你本机可用的 Python 3.10+。

1. 检查 `config.example.json`：`base_url` 默认指向 `http://127.0.0.1:11434/v1`，模型名默认为 `qwen3:8b`；确认 `ollama_context_tokens` 不超过本机模型支持的窗口。
2. 本机 Ollama 不需要密钥；使用远程 HTTPS 接口时，再在终端设置 `api_key_env` 指定的环境变量。不要把密钥发给别人或写入仓库。
3. 检查环境并运行：

```powershell
python main.py doctor
python main.py chat
python main.py run --problem examples/counter.md --testbench examples/counter_tb.sv --with-baseline
```

`chat` 是推荐入口；`run` 是保留的旧版批处理入口。若直接运行 `python main.py`（不带子命令），也会进入聊天模式。

批处理 `run` 不指定 `--run-dir` 时，程序在配置的 `run_root` 下自动创建新目录，并打印完整路径。也可以指定：

```powershell
python main.py run --problem examples/counter.md --testbench examples/counter_tb.sv --run-dir F:/rtl_agent_runs/counter_01
```

要先理解程序流程而不调用模型、Vivado：

```powershell
python main.py demo --run-dir runs/offline_demo_01
```

demo 故意第一次返回坏代码、第二次修好；所有工具结果都是模拟值。它用于演示“生成→报错→反馈→修复”，不证明 Vivado 或模型已跑通。

## 换成你自己的题目

写一个需求文件，明确顶层名、端口位宽、复位规则和时序行为。在配置中同步修改：

- `top`：设计顶层模块名。
- `tb_top`：测试台模块名，必须与设计顶层不同。
- `clocks`：时钟端口与周期；组合电路用 `[]`。
- `simulation_ns`：最长仿真时间，要足够覆盖固定测试台。
- `stage_timeout_seconds`：每次 Vivado 进程超时上限。

本地模型不生成测试台。若你有可信测试台，失败调用 `$fatal(1, "原因")`，所有断言通过后单独打印 `$display("RTL_AGENT_TEST_PASS")`，再 `$finish`。测试台须实例化被测顶层、验证具体输出，并有 watchdog。仅进程退出码为 0 不算仿真通过。

```powershell
python main.py run --problem my_task.md --testbench my_tb.sv
```

没有测试台时，省略 `--testbench`，Agent 会执行 RTL 编译/展开和综合，并在结果里标记功能仿真跳过：

```powershell
python main.py run --problem my_task.md
```

这能检查 RTL 是否可展开、可综合，但不能证明功能正确，也不能取代官方参考测试。命令行中的 `--self-test` 已停用。

附加自己的可信 XDC 可使用 `--xdc extra.xdc`。程序不让模型修改时钟或 XDC；没有板级 I/O 管脚约束也可以做本项目要求的编译/仿真/综合。

本版面向小型、纯 RTL 模块，支持 `rtl/` 下多个 `.v/.sv/.vh/.svh` 文件和相互 include，不包含 IP Catalog、Block Design、外部内存初始化文件、DPI 或上板设计。

## Vivado 怎么被调用

Python 生成 `run.tcl`，通过如下形式启动命令行：

```text
vivado.bat -mode batch -notrace -source run.tcl
```

与 GUI 中 Tcl Console 使用同一种 Tcl 命令，不需要鼠标操作窗口。

| 阶段 | 主要 Tcl | 成功检查 |
|---|---|---|
| compile | `create_project`、`add_files`；有测试台时编译/展开仿真层次，无测试台时 `synth_design -rtl` 做 RTL 展开检查 | 正常退出、阶段结束标志、日志无 ERROR/FATAL |
| simulation | `open_project`、`launch_simulation -step simulate` | 以上条件，以及仿真日志中独立的测试通过标志 |
| synthesis | `open_project`、`synth_design`、`write_checkpoint`、`write_verilog` | 以上条件，以及非空网表与 DCP；确认配置时钟已生效 |

编译流程由 Vivado 调用 xvlog/xelab，仿真由 xsim 执行。综合后保存资源和时序报告；负时序裕量记录在报告中，不额外冒充比赛的通过条件。聊天模式由模型选择写文件、读取报告或修复；RTL 保存后 Python 自动执行固定验证和导出。**不是计数器硬编码，也不是允许模型执行任意 Tcl 或 shell。** 后续可在 `vivado.py` 中增加工具命令。

## 你要求的上下文管理

- 当前需求、固定测试台状态、时钟/XDC 约束摘要始终放入模型输入。完整 RTL 和日志留在磁盘，需要时按需读取相关行，不把大文件反复塞入上下文。
- 未解决诊断的完整原文保存在磁盘；上下文只携带有行号的错误节选，模型可用 read_report 分页读取。最近一次读取结果保留给下一轮模型。
- 只有同一阶段重新通过，才标记该阶段的历史错误已解决。模型说“修好了”不算。
- 上下文预算不够时，按时间先后把旧 Tcl 调用及成功输出折叠为“做过什么、哪个阶段、成功/失败、日志在哪”。
- 如果压缩历史后仍装不下固定内容和未解决错误，程序保存现场并暂停；不会丢弃报错强行继续。

本版使用偏保守的文本 token 估算，不是 Qwen 官方分词器。`context_window_tokens` 是应用预算，不是程序宣称模型的官方窗口；本机 Qwen 请求同时使用较小的 `ollama_context_tokens`。默认 `adaptive_output_tokens=false`，使用 `max_output_tokens` 作为固定输出预算；大型设计应使用 `write_design_chunk` 分块写盘。设置为 `true` 时，每次请求会按剩余窗口自适应输出预算。`max_output_tokens=0` 只在自适应模式下有意义。服务端也可能因额外开销拒绝请求，此时同样保留现场。

## 中断和恢复

聊天中 `/quit` 保存退出，恢复时使用启动时显示的完整会话目录：

```powershell
python main.py chat --session-dir F:/rtl_agent_runs/chat_实际会话名
```

上面的会话名须替换为已有英文目录名，不要直接复制中文占位符。PowerShell 启动脚本也支持 `-SessionDir`。普通对话、代码、固定测试台和未解决报错均保留，恢复后继续输入消息。每个任务使用独立的 `chat_<task>_<时间戳>/` 目录，包含自己的 RTL、测试台、草稿、Vivado 工程和报告。`/new gcd` 创建 GCD 任务，`/new counter` 创建计数器任务；任务之间不共享 `current_design`、测试台或错误记录。若你自带可信测试台，在新会话启动时用 `--testbench my_tb.sv`（或启动脚本的 `-Testbench`）。

聊天模式的 `max_chat_steps` 限制每条用户消息的模型调用次数，`max_attempts` 限制每条消息的代码修改次数。工具执行时 Ctrl+C 会停止该次工具进程并尽量保存完整日志；API 出错不会切换到离线演示。

以下为旧版批处理恢复方式，不能用于聊天目录：

```powershell
python main.py status --run-dir F:/rtl_agent_runs/counter_01
python main.py resume --run-dir F:/rtl_agent_runs/counter_01 --additional-attempts 2
```

恢复保留原题、当前设计和未解决错误，再生成修复版，并从编译重新检查全部阶段；不会尝试恢复半途停止的 Vivado 内存状态。这样修复后不会跳过先前已通过但可能被改坏的阶段。

达到总次数上限、同样错误连续 3 次、工具超时或 API 异常都会留下日志；安装、路径或许可证错误直接暂停，避免浪费模型调用。强制关闭终端后若留下 `.run.lock`，确认进程已退出才能手动删除该锁。

## 输出在哪里

聊天模式使用独立的、可恢复目录：

```text
<会话目录>/
  chat.json                 对话、当前版本、检查状态和文件校验值
  inputs/                   固定测试台和 XDC
  model/0001/               每次真实 API 请求、回答和上下文元数据
  revisions/001/            当前版本 RTL、Vivado 阶段日志和报告
  exports/v001/              finish_design 导出的已验证结果
```

旧版批处理 `run` 使用下面的目录结构：

```text
<运行目录>/
  inputs/                   原始需求、冻结测试台、固定 XDC
  state.json                当前状态、未解决错误、文件校验值
  events.jsonl              每轮工具执行记录
  baseline/                 可选：同模型单次裸生成原始回答
  attempts/001/
    model/request.json      实际发给模型的输入，无密钥
    model/answer.txt        模型回答
    model/context_meta.json 上下文估算和被压缩操作数量
    design.json             本轮完整设计
    rtl/                    本轮代码
    project/                本轮 Vivado 工程
    stages/<阶段>/          run.tcl、原始日志、完整诊断、判定
    reports/                综合网表、DCP、资源/时序报告
  final/                    全部通过后导出的 RTL、测试台、报告和 result.json
```

`--with-baseline` 会在同一运行先生成裸模型结果，仅把原始题目发给相同模型，不调用工具、不带技能、不做应用层重试。当前保留原始回答，不自动计算 pass@1/pass@5；批量评测和官方输入输出适配需等具体评测协议接入。

## 读代码的顺序

1. `rtl_agent/chat.py`：先看 `interactive_chat` 和 `ChatSession.send`，理解输入、模型回答、工具执行、结果回传。
2. `rtl_agent/chat_tools.py`：看提供给模型的工具定义和使用规则。
3. `rtl_agent/llm.py`：看 Python 如何发真实模型请求和接收原生工具调用。
4. `rtl_agent/vivado.py`：看 Tcl 生成和日志判定。
5. `rtl_agent/context.py`：看旧版批处理上下文压缩。
6. `rtl_agent/design.py`：看模型文件输出的校验约定。

`rtl_agent/workflow.py` 是另外保留的批处理流程，不是默认聊天入口。

## 验证和当前边界

### 本机 Qwen3 简单任务验收

先用固定计数器题目和可信测试台，避免模型生成错误测试台导致假失败：

```powershell
python main.py chat --example counter
```

启动后会真实请求配置中的模型生成 RTL。生成完成后输入 `/verify`，Python 按固定顺序执行
compile、simulation、synthesis；全部通过才导出到当前会话的 `exports/v001/`。此命令不请求模型，
失败时保留完整日志，不伪造成功，也不自动修改冻结测试台。`/new` 创建的是普通空任务，不会沿用示例测试台。

本地模型只生成 DUT RTL。生成并要求验证后，Python 会自动执行 Vivado RTL 展开检查与综合；没有测试台时跳过功能仿真，不能据此判断功能正确。需要功能验收时，请使用可信测试台或比赛官方测试器。

重复真实验收可运行：

```powershell
python tests/ollama_smoke.py --work-dir F:/rtl_agent_runs/counter_smoke_new --repeat 3 --vivado
```

必须选择不存在的新目录。该脚本真实请求配置的模型，`--vivado` 还会真实运行三个阶段；远程接口可能收费。
`summary.json` 记录每次生成调用次数、耗时、验证结果和导出目录。未加 `--vivado` 时只检查生成落盘，
不能声称功能验证通过。固定计数器样本的成功率不是任意 RTL 题目的总体成功率。

```powershell
python -m unittest discover -s tests -v
python tests/vivado_smoke.py --work-dir F:/rtl_agent_runs/smoke_01
```

第二条调用真实 Vivado，但用固定模型回答测试“首轮语法错误→第二轮修复→编译/仿真/综合”，不调用 Ollama。请选择不存在的新运行目录。

实际交付测试记录见 `VALIDATION.md`。没有实测成功的部分不应当作已验证。程序不是操作系统沙箱，请使用可信测试台/XDC和普通用户账户。模型生成代码的检查是基础防护，不构成恶意代码隔离。

本项目默认使用本机 Ollama 的开源模型；工程不包含模型权重、GPU 驱动或 Ollama 安装脚本。实际比赛环境仍需单独确认模型、工具权限和 Vivado 许可证满足赛题要求。

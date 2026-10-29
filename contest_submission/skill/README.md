# 技能包

技能包是领域知识的组织形式。它不是提示词的堆叠，评价标准只有一条：**他人能否照着用。**

技能应按任务和错误类别择需加载，不把所有内容堆进每次提示词。每条技能要简短、可执行，
并尽量能指出它对应的失败样本或明确规则；条数本身不代表质量。

---

## 采用 AMD 官方技能格式

`SKILL.md` 的写法与 AMD 官方的智能体技能一致（`hls-flattenable`、`hls-line-buffer`
等）。**正文用英文**，与官方保持一致；仓库其余文档仍为中文。

这样做有一个直接好处：**官方技能可以拷进 `skill/` 目录使用**，
`agent/skills.py` 的加载器对两者通用，已实测。

官方技能仓库（AMD Ross）：https://github.com/Xilinx/ross-ai-assistant

该仓库同时提供 Vitis HLS 与 Vivado RTL 技能。当前提交包把
`vivado-rtl-elaboration-analysis` 的首错定位与最小修复方法适配进
`vivado-synthesis-diagnostics/SKILL.md`；它保留 AMD 的 MIT 版权/许可标识，并把 MCP 专用步骤
改写为比赛容器现有的本地 `xvlog`、`xelab` 和 Vivado batch synthesis 流程。HLS 架构技能不适用
本次 RTL-only 隐藏测试台流程，所以没有加载进竞赛运行时。

公开研究参考：

- RTLFixer：检索加编译反馈的有限修复循环，https://arxiv.org/abs/2311.16543
- VerilogEval：用 golden simulation 评估 RTL 行为，https://arxiv.org/abs/2309.07544
- VerilogEval 更新版：提示工程和失败分类对结果重要，https://arxiv.org/abs/2408.11053

这里只把方法作为设计依据，不将论文报告数字当作本方案成绩。实际效果要由同模型、同题集的
官方判定器 A/B 结果确认。

Ross 仓库覆盖 HLS 与 Vivado RTL。此提交只采用和比赛运行环境兼容的 RTL elaboration
诊断原则；要求 Vivado MCP 的完整交互流程、HLS C++ 架构流程与隐藏测试台无关，未加入运行时。

---

## 目录结构

```
skill/
├── README.md
└── <skill-name>/
    └── SKILL.md
```

一条技能一个目录。目录名与文件名一律英文小写加连字符。

## `SKILL.md` 的格式

````markdown
---
name: rtl-module-interface-contract
description: 一行说清楚解决什么问题。单行，不用 YAML 块标量。
---

<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Agent Skill: <Title>

## Skill Metadata
- **Name:** `rtl_module_interface_contract`
- **Description:** 与 frontmatter 一致
- **Trigger:** 什么情况下该用这条技能（散文）
- **Log signatures:** `VRFC 10-3180`, `cannot find port`      ← 见下

## System Prompt
## Rules            ← 判定类技能：Rule 1 … Rule N，逐条可判 PASS/FAIL
## Steps            ← 操作类技能：Step 1 … Step N，逐步给出做法与理由
## Evidence         ← 实测数据，须标注工具版本、器件、测量口径
## Output Format
## Behavioral Constraints
````

frontmatter 只有 `name` 与 `description` 两项，其余信息放正文，这是官方的写法。

### `Log signatures` 是本仓库加的一行，不属于官方格式

官方技能由一个读过技能描述的智能体来挑选；本示范智能体是拿工具输出去匹配的，
而散文式的 Trigger 不会出现在 `xelab` 的日志里。

因此约定：`- **Log signatures:**` 这一行里用反引号括起来的字符串，会被
`agent/skills.py` 逐个拿去在日志里做子串匹配，命中才注入上下文。没有这一行的技能
（包括所有官方技能）照常加载，只是不会因为某条错误而自动触发。

---

## 写技能的四条经验

**一、「何时不用」比「何时用」重要。** 一条不说明边界的技能，模型会在不适用的场合
照搬，引入新的失败。示范技能的 `Behavioral Constraints` 一节就是干这个的：明确写出
哪些做法不许用（例如不许用别名 wire 绕开端口名不符、不许为了对齐接口而删掉
输出端口上的 `reg`）。

**二、记录已证伪的做法。** 「试过 X，无效，因为 Y」这类条目直接节省综合轮次，而综合
轮次就是墙钟，墙钟就是分数。

**三、实测数据必须标注口径。** 一个漂亮的数字没有用，一个标注了工具版本、器件、
测量方式的数字才能被复用。示范技能的 `Evidence` 一节写明了「Vivado 2026.1，
`xczu3eg-sbva484-1-e`，与参考测试台一并详细描述」，而不只是给一张表。

**四、技能不必涉及大模型。** 确定性的校验脚本、结构化的踩坑清单，都是技能。
`agent/tools.py::check_interface()` 就是一例：纯文本比较，不调模型不调工具，
却覆盖了整个 L0 失败类。评分标准是可复用性，不是是否调用模型。

---

## 几个值得写成技能的方向

以下为 RTL 侧常见的失败聚集处，供参考。该列表不应直接照抄提交，缺少实证支撑的
技能条目在工程质量项上不得分。

| 方向             | 典型失败                                                   | 落在哪一级 |
| ---------------- | ---------------------------------------------------------- | ---------- |
| 模块接口契约     | 模块名、端口名、方向、位宽不符                             | L0（本目录示范） |
| 复位语义         | 同步写成异步、极性反了、复位值错                           | L1         |
| 阻塞与非阻塞     | 时序逻辑用 `=`、组合逻辑用 `<=`，仿真与综合行为不一致      | L1         |
| 敏感列表         | `always @(a)` 漏了信号，或组合逻辑分支没写全产生锁存器     | L1 / L2    |
| FSM 编码         | 状态转移对了但输出时序差一拍（Moore 与 Mealy 混淆）        | L1         |
| 位宽与符号       | 隐式截断、有符号无符号混用、`$signed` 用法                 | L1         |
| 多驱动           | 同一信号在两个 `always` 块里赋值                           | L2         |
| 不可综合构造     | `real`、延时、文件 IO、`fork/join`                         | L2         |
| 题面含图         | 波形图、状态转移图在纯文本题面里丢失信息                   | L0 / L1    |
| 功能正确性       | 把题面转成逐拍行为和边界条件；官方隐藏测试台负责最终判定    | L1 → L2；提交 Agent 无参考测试台 |

官方功能测试台不会下发给 Agent，因此提交程序无法用独立 oracle 完成 L2 自测。
本技能包只能帮助模型准确提取逐拍语义与边界条件；本地 lint/综合通过不能代替官方仿真判定。

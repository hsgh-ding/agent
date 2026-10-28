# MODEL.md

> 云端实测前的工作副本。`manifest.json` 中 revision、显存实测值和 vLLM 版本目前为
> `null`；**不要把这份状态当作最终封存版本**。模型下载后按下方命令固定 commit，启动服务后
> 记录 vLLM 版本与 `rocm-smi` 显存，再同步填写本文件和 manifest。

> 模板。所填内容须与 `manifest.json`、`REPORT.md` 第 2 节一致。
>
> **本文件未如实填写时，增益项 40 分不得分**（评分细则 2.4）。

---

## 1. 模型

| 项           | 内容                                    |
| ------------ | --------------------------------------- |
| 模型名称     | Qwen/Qwen3-8B                           |
| 来源         | https://huggingface.co/Qwen/Qwen3-8B    |
| 版本号 / SHA | 待 Radeon Cloud 下载时固定的 Hugging Face commit SHA |
| 权重校验和   | `sha256: ...`（无版本号时必填）         |
| 参数量       | 8B                                      |
| 许可证       | Apache-2.0                              |

**必须是可本地部署的开源权重。** 评测时方案须断网运行，商用 API 在评分细则 2.1 与
2.3 两条上均不成立，全部运行类得分无法产生。

## 2. 量化与显存

| 项           | 内容                        |
| ------------ | --------------------------- |
| 量化方式     | 无 / AWQ / GPTQ / GGUF-Q4_K_M / ... |
| 量化工具与版本 |                           |
| **实测显存占用** | 待 Radeon Cloud 启动后测量（单卡，稳态） |
| 测量方式     | `rocm-smi --showmemuse`     |
| 测量硬件     | W7900                       |

**上限单卡 32 GB，不允许跨卡。** 云实例的 W7900 有 48 GB，但规则统一按 32 GB 计，超出的
运行不计入统计（评分细则 2.2）。

参照：32B 模型 FP16 约 64 GB（装不下），4-bit 量化后约 18 GB；7B 模型 FP16 约 14 GB。

## 3. 上下文配置

| 项                 | 值    |
| ------------------ | ----- |
| `max_model_len`    | 32768 |
| 单次输出上限       | 16384 |
| RoPE 外推方式      | 无 / YaRN factor=... |
| 思考设置           | 关闭（`manifest.json` → `generation`：`enable_thinking: false`） |

**本项不受统一约束，但必须如实声明。** 思考设置须与 `manifest.json` 的 `generation` 段一致，
`baseline.py` 按那里的声明发请求，方案也须使用同一套设置。

上下文长度对结果的影响是一阶的，不是可以含糊带过的参数。已有评测记录到：同一推理
模型在 8 K 输出窗口下因截断表现很差，放宽到 16 K 后通过率大幅跳升，而截断样本仅占
5.31%；另一项工作中统一的 4096 输出上限静默截断了七个受评模型中的三个，放宽后有
模型从第五名升至第一。

**基线运行必须使用与方案完全相同的上下文配置**，否则方案与基线不可比。

## 4. 推理栈

| 项           | 内容                    |
| ------------ | ----------------------- |
| 后端         | vLLM / llama.cpp / SGLang / ... |
| 版本         | 待在 Radeon Cloud 运行 `python -m pip show vllm` 后填写 |
| 关键启动参数 | 见 `serve/vllm.sh`      |
| 并发策略     | 每卡一实例 / 单实例批处理 / ... |

## 5. 微调

**未微调**填「否」，本节结束。

微调则以下各项均为必填（评分细则 2.5，缺失则增益项不得分）：

| 项             | 内容 |
| -------------- | ---- |
| 基座模型       |      |
| 微调方式       | 全参 / LoRA / QLoRA，rank、学习率、轮数 |
| 训练数据来源   | 须可核查                                |
| 训练数据规模   |                                         |
| **微调权重开源地址** | 链接须可达                        |

## 6. 辅助模型

方案在主模型之外使用的其他模型（判别、打分、检索、嵌入等）逐个列出；**没有**填「无」，
本节结束（评分细则 2.13）。

| 项             | 内容 |
| -------------- | ---- |
| 名称与用途     |      |
| 基座 / 参数量  |      |
| 运行位置与显存 | GPU / CPU，实测显存计入单卡 32 GB |
| 是否经训练或微调 | 是则写明方式、数据来源与规模 |
| 权重位置       | 随封存版本提交，或仅赛事方可访问的地址 |

## 7. 声明

- [ ] 上述模型为可本地部署的开源权重，非商用 API
- [ ] 实测显存占用（含辅助模型）不超过 32 GB，单卡运行
- [ ] `run_baseline.sh` 使用与 `run.sh` 完全相同的模型、量化方式与上下文配置
- [ ] 若经微调，权重已开源且链接可达
- [ ] 本文件内容与 `manifest.json`、`REPORT.md` 一致

## 首次下载与记录

在赛道提供的 Radeon Cloud Jupyter Notebook 里运行。模型仓库约 16 GB，下载到持久目录，
不要把权重提交到 Git：

```python
from huggingface_hub import HfApi, snapshot_download

repo_id = "Qwen/Qwen3-8B"
revision = HfApi().model_info(repo_id, revision="main").sha
model_dir = "/workspace/models/Qwen3-8B"
snapshot_download(repo_id=repo_id, revision=revision, local_dir=model_dir)
print("下载目录:", model_dir)
print("固定 revision:", revision)
```

把打印出的 revision 写入 `manifest.json` 的 `model.revision` 和本页版本号。随后按
`CLOUD_JUPYTER_GUIDE.md` 启动 `serve/start.sh`，用 `python -m pip show vllm` 记录实际版本，
用 `rocm-smi --showmemuse` 记录模型加载并完成一次请求后的显存占用；将该值同时写入
`manifest.json` 的 `model.measured_vram_gb` 和上面的表格。权重路径应设置为：

```bash
MODEL=/workspace/models/Qwen3-8B ./serve/start.sh
```

# serve/

推理服务与投题接口。两者跑在同一个实例里，互不干扰。

```
  8000   vllm.sh        推理服务   绑 127.0.0.1，不暴露
  7860   serve_api.py   投题接口   绑 127.0.0.1，经 rc-tunnel 对外
```

---

## 开发期：不需要 GPU

```bash
export LLM_BACKEND=openai
export LLM_BASE_URL=https://api.deepseek.com/v1     # 或任意 OpenAI 兼容端点
export LLM_API_KEY=sk-xxxxxxxx
export LLM_MODEL=deepseek-coder
```

无需启动 `vllm.sh`，`agent/llm.py` 将直接访问所配置的端点。

## 正式提交：必须是本地权重

```bash
MODEL=/models/Qwen3-8B ./serve/start.sh     # 后台拉起 vllm.sh，就绪后返回

export LLM_BACKEND=openai
export LLM_BASE_URL=http://localhost:8000/v1        # 换成本机
export LLM_API_KEY=dummy
export LLM_MODEL=Qwen3-8B
```

智能体代码无需改动，差异仅在于三个环境变量。但提示词与重试策略未必可以直接平移，商用
API 与本地小模型在输出长度、指令遵循、代码格式稳定性上差异明显，围绕前者调好的东西
换到后者常常直接失效。尽早跑一次本地模型。

## 起投题接口

```bash
export FPGACHINA_TOKEN=<赛事方发的令牌>
export MODEL_NAME=Qwen3-8B
./serve/run_api.sh start        # 后台运行，退出后自动重启；停止用 ./serve/run_api.sh stop

# 让外面能访问到：平台只接管绑在 127.0.0.1 上的 HTTP 服务
/var/run/secrets/frp-self-service/install
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$HOME/.local/bin/rc-tunnel" expose --port 7860   # 去掉代理变量，否则隧道连不上
```

自测：

```bash
curl -H "Authorization: Bearer $FPGACHINA_TOKEN" http://localhost:7860/v1/health
# {"ready":true,"track":"rtl","model":"Qwen3-8B","vram_gb":21.4}

# 投一道题：立即返回 job_id，之后查询直到 status 为 done
curl -X POST -H "Authorization: Bearer $FPGACHINA_TOKEN" -H "Content-Type: application/json" \
     -d '{"task_id":"t1","nonce":"n1","mode":"agent","prompt":"...","interface":"","deadline_s":900}' \
     http://localhost:7860/v1/solve
curl -H "Authorization: Bearer $FPGACHINA_TOKEN" http://localhost:7860/v1/jobs/<job_id>
```

`track` 必须是 `"rtl"`，赛事方投题前会核对，不符即拒投。

---

## 几处容易踩的地方

**显存按 32 GB 预算，而不是按卡的容量。** vLLM 按 `--gpu-memory-utilization` 乘以整卡显存
预先分配，与模型大小无关。验证窗口的 W7900 有 48 GB，若取 0.90 将占用约 43 GB，超出单卡
32 GB 的上限。`vllm.sh` 因此按 `VRAM_BUDGET_GB`（默认 30）换算该比例，也可用 `GPU_UTIL`
直接指定；提交前须以 `rocm-smi --showmemuse` 实测稳态占用。

**绑 `127.0.0.1`，不是 `0.0.0.0`。** Radeon Cloud 的 `rc-tunnel` 只接管绑在环回口上的
HTTP 服务，绑 `0.0.0.0` 平台不接管。绑错了服务本身是活的，但外面永远打不进来，在赛事方
那边看起来和挂掉没有区别，而失败不重投。

**只暴露 7860 一个端口。** 平台限制一个 notebook 同时只能暴露一个端口。vLLM 的 8000
由智能体经 `127.0.0.1` 内部调用，不要去暴露。**同时参加两个赛道的队伍需要两台 VM。**

**令牌检查不是可选项。** 平台给出的公网 app URL 不带业务层鉴权，没有令牌检查，任何人
拿到网址都能投题。

**工程与中间文件落本地目录。** `AGENT_TMP` 默认 `/tmp`；实例上 `/tmp` 是 4 GB 内存盘，请设为 `/var/tmp`。云平台的 `/workspace` 是 NFS，
Vivado 综合产生的大量小文件写在上面极慢。这不是优化建议。

**服务要能连续跑数百次请求不重启。** 评测期间挂了就是 L0，赛事方不重投。注意每题之后
清理临时目录与子进程，别把磁盘写满。

**投题是异步的。** `POST /v1/solve` 立即返回任务编号，题在后台解，赛事方每 2 秒查询一次。
平台入口会切断 60 秒内没有响应的请求，所以自己改写投题服务时，不能让请求挂着等解题完成。

**自己先超时，别让赛事方超时。** `serve_api.py` 在 `deadline_s - 10` 秒时对 `run.sh`
整个进程组发 SIGTERM，5 秒后 SIGKILL，然后返回已有的最好结果。赛事方侧超时一定是 L0，
自己先停至少还有 L1/L2 的可能。要按进程组结束，是因为只杀 `run.sh` 一个进程时，它启动的
智能体与 Vivado 会继续运行，和下一题争抢 GPU 与 CPU。

**常驻进程只从 `start.sh` 起。** 推理服务与队伍自己的常驻引擎都写进 `start.sh`，用
`setsid` 拉起。容器重建这条退路下，赛事方只运行 `start.sh` 再逐题调用 `run.sh`，不启动
`serve_api.py`；超时信号发给 `run.sh` 的进程组，由 `run.sh` 在后台带起的常驻进程会随之
被结束。详见 `docs/API_CONTRACT.md` 第八节。

**进程要自己回收。** 平台实例的 1 号进程是 JupyterLab，它不回收孤儿进程，被结束的子进程
会作为僵尸一直占着进程号，攒多了新进程就起不来。`serve_api.py` 启动时把自己设为回收者
（`PR_SET_CHILD_SUBREAPER`），每题结束后回收该题的全部进程。换成自己的服务时这一段
要保留。详见 `docs/API_CONTRACT.md` 第十节。

---

## W7900（gfx1100）上的已知问题

初赛与决赛都在 W7900 上运行。该架构**没有 FP8 矩阵指令**，FP8 量化只省显存、不获加速；
选择量化方式时以实测吞吐为准。

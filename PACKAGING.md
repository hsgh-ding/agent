# 更新现有提交包

本次直接更新的目录是 `F:/codex_task/fpga/rtl_agent/contest_submission/`。
后续以这里的文件为准，不需要使用上一轮另建的 `agent_submission_repo/`。

本目录的布局：

```text
run.sh                 比赛入口
agent/                 比赛 Agent
serve/                 模型和投题接口
dev/local_agent/       最新本地聊天 Agent
tools/                 源码打包与检查工具
```

在本目录运行：

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tools -p "test_*.py" -v
python tools/package_release.py --output ../contest_submission_20261029.zip
```

开发版测试单独运行：

```powershell
cd dev/local_agent
python -m unittest discover -s tests -q
```

ZIP 解压后直接看到 `run.sh`、`agent/`、`dev/`，不会多套一层目录。
打包器排除 `.git`、缓存、日志、模型权重、`.env`、本地私有配置和运行工程。
原始基线文件不修改。`RELEASE_MANIFEST.json` 包含包内文件 SHA-256。

## 更新你的 GitHub 和 Jupyter

你的 GitHub 仓库 `hsgh-ding/agent` 把比赛包放在 `contest_submission/` 下。
将本目录内容覆盖到仓库里**已有的** `contest_submission/`，不要覆盖到仓库根目录，
也不要生成 `contest_submission/contest_submission/`。
如果旧仓库还有 `contest_submission/agent/rtl_agent/`、`contest_submission/agent/skills/`
或提交过的 `__pycache__/`，同步删除这些旧文件；网页覆盖上传不会自动删除它们。

提交并推送后，在 JupyterLab Terminal 中运行：

```bash
cd /workspace/agent
git pull --ff-only
cd contest_submission
chmod +x run.sh run_baseline.sh serve/*.sh
python3 -m unittest discover -s tests -v
```

完整的 Qwen3-8B/vLLM 和官方评测操作见 `JUPYTER_GUIDE.md`。
本地自动测试不代表真实模型端到端成功或比赛得分；模型版本和实测显存仍需在云端如实填写。

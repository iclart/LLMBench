# 在 Linux 虚拟机里用 OpenCode 跑一次评测

本目录的工具负责“让 Agent 做题”的部分：打包工作区、在时限内运行 Agent、定时快照、提前交卷和到点强制收卷。评分用 `../eval/railway_eval.py`。

| 文件 | 作用 |
|---|---|
| `PROMPT.md` | 发给 Agent 的提示词 |
| `package_workspace.py` | 打包工作区：`TASK.md`、接口头文件、空实现、`Makefile`、预编译的压测器库和工具、公开数据与标准答案；带源码白名单检查 |
| `workspace/Makefile` | 工作区里的 `make` / `make check` / `make bench` / `make time` / `make submit` |
| `run_opencode.py` | 运行 OpenCode：快照、提前交卷、强制收卷、自动续跑、统计 token 和费用 |
| `vm_netguard.sh` | 用 nftables 只允许 Agent 账户访问指定的 API 域名 |

## 0. 准备虚拟机（一次）

需要：x86-64 Linux、g++ ≥ 13、CMake ≥ 3.20、Python ≥ 3.12、Node.js/npm、nftables、bubblewrap、sudo。

```sh
git clone --recurse-submodules https://github.com/iclart/LLMBench.git && cd LLMBench
cmake -S . -B build && cmake --build build -j && ctest --test-dir build -j4   # 确认全部通过
npm install -g opencode-ai          # 已用 1.19.3 测试；换版本后先确认 `opencode run --help` 里仍有 --dir/--model/--format/--session/--auto
sudo useradd -m llmagent            # Agent 专用账户
chmod 750 ~                         # 确保 llmagent 读不到 LLMBench（里面有强参考和隐藏配置）
```

把模型提供商的 API 密钥写进一个只有你能读的文件，例如 `~/keys.env`（`chmod 600`）：

```
ANTHROPIC_API_KEY=...
```

用 `opencode models` 查看可用的模型名，格式是 `provider/model`。

## 配置模型

`--model` 是 OpenCode 的模型 ID，格式为 `provider/model`，用 `opencode models`（或 `opencode models <provider>`）查看；列表来自 models.dev，`opencode models --refresh` 可以更新。运行器会把它同时写进本次运行配置的 `model` 和 `small_model`，避免 OpenCode 偷偷调用别家的小模型来起标题、做摘要（那部分用量不会算到被测模型头上，也会被网络限制挡住）。

**API 密钥只能用 `--env-file` 传入。**运行器给 Agent 单独的 `HOME`，所以你用 `opencode providers login` 保存的凭据它看不到。常见的环境变量：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY` 等，以 OpenCode 对应提供商的要求为准。

**自定义提供商**（中转站、自部署的 OpenAI 兼容接口等）写成 JSON 文件，用 `--opencode-config` 合并进本次运行的配置，例如：

```json
{
  "provider": {
    "myproxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "My endpoint",
      "options": { "baseURL": "https://llm.example.com/v1", "apiKey": "{env:MYPROXY_API_KEY}" },
      "models": { "my-model": { "name": "My Model", "limit": { "context": 128000, "output": 16000 } } }
    }
  }
}
```

然后 `--model myproxy/my-model`，并在 `--env-file` 里写上 `MYPROXY_API_KEY=...`。这个文件里的 `permission` 设置会被忽略，规则不能被放宽；别忘了把 `llm.example.com` 加进 `vm_netguard.sh` 的放行域名。

## 1. 打包工作区

```sh
python3 tasks/railway/agent/package_workspace.py --out ~/llmbench/workspace --selftest
```

`--selftest` 会确认空实现能编译、基线能通过 `make check`（需要几分钟）。

## 2. 运行 Agent

```sh
sudo tasks/railway/agent/vm_netguard.sh enable llmagent api.anthropic.com models.dev   # 按你的提供商改域名
sudo python3 tasks/railway/agent/run_opencode.py \
    --workspace ~/llmbench/workspace --out ~/llmbench/runs/run1 \
    --model <provider/model> --user llmagent --env-file ~/keys.env
sudo tasks/railway/agent/vm_netguard.sh disable
```

- 默认 150 分钟，每 30 分钟快照到 `runs/run1/snapshots/030`、`060`……
- **提前交卷**：Agent 在工作区运行 `make submit`，几秒内结束，最后一张快照名为 `<分钟>_final`。
- **强制收卷**：到点先杀掉 `llmagent` 的全部进程（包括逃逸到后台的），再拍最后一张快照，之后 `src/` 不可能再变。
- Agent 没交卷就自己停下时，会用同一个会话续跑，并告知剩余分钟数（最多 `--max-resumes` 次）。
- 结束后 `runs/run1/run.json` 记录结束原因、用时、续跑次数、token 和费用；`logs/` 里有完整事件流和会话导出。
- 没有 `--user` 也能跑（以当前用户运行），但那样 Agent 能读到 LLMBench 仓库，结果只能当作调试，不算正式成绩。

## 3. 评分

```sh
E="python3 tasks/railway/eval/railway_eval.py"
$E prepare   --season seasons/s1 --configs private/railway/hidden --seed <赛季种子>   # 每季一次，较慢
$E snapshots --season seasons/s1 --snapshots ~/llmbench/runs/run1/snapshots --out ~/llmbench/runs/run1/results
```

`results/curve.json` 是分数随时间变化的曲线；最终成绩在最后一张快照的结果里。

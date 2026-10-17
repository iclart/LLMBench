# 在 Linux 虚拟机里用 OpenCode 跑一次评测

本目录的工具负责“让 Agent 做题”的部分：打包工作区、在时限内运行 Agent、定时快照、提前交卷和到点强制收卷。评分用 `../eval/railway_eval.py`。

| 文件 | 作用 |
|---|---|
| `PROMPT.md` | 发给 Agent 的提示词 |
| `package_workspace.py` | 打包工作区：`TASK.md`、接口头文件、空实现、`Makefile`、预编译的压测器库和工具、公开数据与标准答案；带源码白名单检查 |
| `workspace/Makefile` | 工作区里的 `make` / `make check` / `make bench` / `make time` / `make submit` |
| `run_opencode.py` | 通过 OpenCode v2 API 运行 Agent：快照、时间提醒、提前交卷、强制收卷、自动续跑、统计 token 和费用；网页实时观看 |
| `run_reference.py` | 用参考实现代替 LLM 空跑整条流程 |
| `vm_netguard.sh` | 用 nftables 只允许 Agent 账户访问指定的 API 域名 |

## 0. 准备虚拟机（一次）

需要：x86-64 Linux、g++ ≥ 13、CMake ≥ 3.20、Python ≥ 3.12、Node.js/npm、nftables、bubblewrap、sudo。

```sh
git clone --recurse-submodules https://github.com/iclart/LLMBench.git && cd LLMBench
cmake -S . -B build && cmake --build build -j && ctest --test-dir build -j4   # 确认全部通过
npm install -g opencode-ai          # 已用 1.19.3 测试；换版本后先确认 `opencode run --help` 里仍有 --dir/--model/--format/--session/--auto
sudo useradd -m llmagent            # 正式模式的专用账户
chmod 750 ~                         # 确保 llmagent 读不到 LLMBench（里面有强参考和隐藏配置）
```

把模型提供商的 API 密钥写进一个只有你能读的文件，例如 `~/keys.env`（`chmod 600`）：

```
ANTHROPIC_API_KEY=...
```

用 `opencode models` 查看可用的模型名，格式是 `provider/model`。

## 配置模型

`--model` 是 OpenCode 的模型 ID，格式为 `provider/model`，用 `opencode models`（或 `opencode models <provider>`）查看；列表来自 models.dev，`opencode models --refresh` 可以更新。运行器会把它同时写进本次运行配置的 `model` 和 `small_model`，避免 OpenCode 偷偷调用别家的小模型来起标题、做摘要（那部分用量不会算到被测模型头上，也会被网络限制挡住）。

**API 密钥只能用 `--env-file` 传入。**运行器给每次运行单独的 OpenCode 配置目录，所以你用 `opencode providers login` 保存的凭据它看不到。常见的环境变量：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY` 等，以 OpenCode 对应提供商的要求为准。

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

## 2. 先用参考实现空跑一遍（推荐）

不花一分钱就能确认整条流程在你的虚拟机上能跑通：

```sh
python3 tasks/railway/agent/run_reference.py --workspace ~/llmbench/workspace --out ~/llmbench/runs/ref_strong --engine strong
python3 tasks/railway/agent/run_reference.py --workspace ~/llmbench/workspace --out ~/llmbench/runs/ref_base --engine baseline --skip-check
```

它把参考实现放进工作区的 `src/`，在工作区里跑 `make check`（证明打包的工具链在这台机器上可用），并生成和真实运行一样的目录结构。按第 4 步评分，强参考应在 100 分左右，基线在 0 分左右。

## 3. 运行 Agent

运行器通过 OpenCode 的 v2 HTTP API（`opencode serve`）驱动 Agent，有两种模式：

| | 试跑模式（默认） | 正式模式（`--strict`） |
|---|---|---|
| 以谁的身份运行 | 你自己 | 专用账户 `llmagent`（需要 root） |
| 工具权限 | OpenCode 权限配置：禁止网页抓取和搜索、禁止向人提问、文件工具不能出工作区 | 同左 |
| 能否拦住 `bash` 联网或读别处的文件 | 不能 | 能：`vm_netguard.sh` 限制网络，专用账户读不到你的文件 |
| 成绩 | 只作调试 | 正式成绩 |

```sh
# 试跑
python3 tasks/railway/agent/run_opencode.py --workspace ~/llmbench/workspace --out ~/llmbench/runs/try1 \
    --model <provider/model> --env-file ~/keys.env

# 正式
sudo tasks/railway/agent/vm_netguard.sh enable llmagent api.anthropic.com models.dev   # 按你的提供商改域名
sudo python3 tasks/railway/agent/run_opencode.py --workspace ~/llmbench/workspace --out /srv/llmbench/run1 \
    --model <provider/model> --env-file ~/keys.env --strict
sudo tasks/railway/agent/vm_netguard.sh disable
```

正式模式下，`--out` 的每一级父目录都必须能被 `llmagent` 进入（比如放在 `/srv/llmbench` 下）；运行器启动前会检查，不满足就直接报错。

**实时观看**：用浏览器打开 `http://<虚拟机地址>:4096/`，用户名 `opencode`，密码在启动时打印，也保存在 `<out>/web.txt`。服务监听 `0.0.0.0`，没有密码访问会被拒绝（401）。只看，不要在网页里输入内容，否则会干扰评测。端口和密码可以用 `--port`、`--password` 指定。

**时间**：
- 提示词里写明开始时间和强制收卷时间；
- 每到快照点（30、60、90、120 分钟），以及收卷前 10 分钟和 2 分钟，运行器会把“现在几点、还剩几分钟”插进正在进行的会话；
- Agent 随时可以运行 `make time`。

**交卷与收卷**：
- Agent 运行 `make submit` 即提前交卷，几秒内结束；
- 到点时先中断会话、杀掉 Agent 的全部进程，再拍最后一张快照，之后 `src/` 不会再变；
- Agent 没交卷就停下时，会被告知剩余时间并继续（最多 `--max-resumes` 次，连续 3 次一续跑就立刻停下则判为卡死并结束）。

结束后 `<out>/run.json` 记录结束原因、用时、续跑次数、token 和费用；`<out>/logs/messages.json` 是完整会话。费用来自 OpenCode 的模型价格数据，自定义提供商如果没有配置价格会显示为 0。

## 4. 评分

```sh
E="python3 tasks/railway/eval/railway_eval.py"
$E prepare   --season seasons/s1 --configs private/railway/hidden --seed <赛季种子>   # 每季一次，较慢
$E snapshots --season seasons/s1 --snapshots ~/llmbench/runs/run1/snapshots --out ~/llmbench/runs/run1/results
```

`results/curve.json` 是分数随时间变化的曲线；最终成绩在最后一张快照的结果里。

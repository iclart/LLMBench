# 在 Linux 虚拟机里用 OpenCode 跑一次评测

本目录的工具负责“让 Agent 做题”的部分：打包工作区、在时限内运行 Agent、定时快照、提前交卷和到点强制收卷。评分用 `../eval/railway_eval.py`，报告用 `../report/report.py`。

Agent 可以用两种方式运行：**OpenCode 自动运行**（`run_opencode.py`，运行器通过 API 驱动 OpenCode）和**人工模式**（`run_manual.py`，任何工具都行，由人按运行器的提示粘贴固定文本）。两者规则相同，成绩进同一张排行榜，人工运行会标注“人工”。

| 文件 | 作用 |
|---|---|
| `PROMPT.md` | 发给 Agent 的提示词 |
| `package_workspace.py` | 打包工作区：`TASK.md`、接口头文件、空实现、`Makefile`、预编译的压测器库（含 TSan 版）和工具、公开数据与标准答案、`make score` 用的计分脚本和本机参考测量；带源码白名单检查 |
| `workspace/Makefile` | 工作区里的 `make` / `make check` / `make bench` / `make tickets` / `make score` / `make tsan` / `make time` / `make submit` |
| `workspace/railscore.py` | `make score`：用官方公式（`eval/scoring.py`）在公开负载上估分 |
| `run_opencode.py` | 通过 OpenCode 的 HTTP API 运行 Agent（支持 OpenCode 1.x 和 2.x）：快照、时间提醒、提前交卷、强制收卷、自动续跑、统计 token 和费用；网页实时观看 |
| `run_manual.py` | 人工模式：用任意 Agent 工具（Claude Code、Codex CLI、Cursor……），运行器负责计时、快照、提醒文本、交卷和收卷 |
| `transcript.py` | 把各工具的会话日志转成统一格式（目前支持 OpenCode 和 Claude Code），供报告使用 |
| `run_reference.py` | 用参考实现代替 LLM 空跑整条流程 |
| `vm_netguard.sh` | 用 nftables 只允许 Agent 账户访问指定的 API 域名 |

## 0. 准备虚拟机（一次）

需要：x86-64 Linux、g++ ≥ 13、CMake ≥ 3.20、Python ≥ 3.12、Node.js/npm、nftables、bubblewrap、sudo。

```sh
git clone --recurse-submodules https://github.com/iclart/LLMBench.git && cd LLMBench
cmake -S . -B build && cmake --build build -j && ctest --test-dir build -j4   # 确认全部通过
npm install -g @opencode/cli       # OpenCode 2（已用 2.0.30 测试）；也可以用 curl -fsSL https://opencode.ai/v2/install | bash
# 或者 OpenCode 1：npm install -g opencode-ai（已用 1.19.3 测试）。运行器按 `opencode --version` 自动适配
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

**推理强度（thinking effort）**用 `--variant` 指定，也可以写成 OpenCode 2 的 `--model provider/model#variant`。可选的档位取决于模型，常见的有 `none`、`low`、`medium`、`high`、`xhigh`、`max`；`opencode models` 不显示档位，运行器启动时会打印当前模型的全部档位，填了不存在的档位会直接报错退出并列出可选值。不指定时用模型的默认档位。同一个模型的不同档位在排行榜上是不同的参赛单位。

自定义提供商需要自己定义档位，在 `--opencode-config` 里给模型加 `variants`（OpenCode 2 写法，`settings` 里的字段原样传给 AI SDK，OpenAI 兼容接口用 `reasoningEffort`）：

```json
{
  "providers": {
    "myproxy": {
      "package": "aisdk:@ai-sdk/openai-compatible",
      "settings": { "baseURL": "https://llm.example.com/v1", "apiKey": "{env:MYPROXY_API_KEY}" },
      "models": {
        "my-model": {
          "limit": { "context": 128000, "output": 16000 },
          "variants": [
            { "id": "high", "settings": { "reasoningEffort": "high" } },
            { "id": "low", "settings": { "reasoningEffort": "low" } }
          ]
        }
      }
    }
  }
}
```

然后用 `--model myproxy/my-model --variant high`。OpenCode 1 的写法是 `"variants": {"high": {"reasoningEffort": "high"}}`（对象而不是数组）。

**API 密钥只能用 `--env-file` 传入。**运行器给每次运行单独的 OpenCode 配置目录，所以你用 `opencode auth` / `opencode providers login` 保存的凭据它看不到。常见的环境变量：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY` 等，以 OpenCode 对应提供商的要求为准。

用 OpenCode 2 时，运行器会把 Agent 执行命令的环境换成只含 `PATH`、`HOME` 等少数变量的最小环境，Agent 在 shell 里读不到这些密钥。OpenCode 1 做不到这一点（它的服务模式不支持替换 shell 环境），Agent 理论上可以用 `env` 看到密钥。无论哪个版本，生成报告时都会把 `--env-file` 里的值从页面中抹掉。

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

然后 `--model myproxy/my-model`，并在 `--env-file` 里写上 `MYPROXY_API_KEY=...`。上面是 OpenCode 1 的写法，OpenCode 2 也认；OpenCode 2 的原生写法是 `providers` 加 `package: "aisdk:@ai-sdk/openai-compatible"` 和 `settings.baseURL`，见它的迁移文档。这个文件里的权限设置（`permission`、`permissions`、`tools`，包括各 agent 下的）都会被忽略，规则不能被放宽；别忘了把 `llm.example.com` 加进 `vm_netguard.sh` 的放行域名。

运行器强制的权限：允许编辑和执行命令；禁止网页抓取、网页搜索、向人提问，以及文件工具访问工作区以外的目录（OpenCode 2 默认允许访问外部目录，运行器会显式禁止）。OpenCode 2 的内置 agent 会在全局规则之后追加自己的规则（比如 build 允许提问、explore 允许联网），所以运行器给每个内置 agent 也加了一份同样的禁止规则。万一还是出现了提问或权限请求，运行器会自动拒绝，不会卡住。

## 1. 打包工作区

```sh
python3 tasks/railway/agent/package_workspace.py --out ~/llmbench/workspace --selftest
```

`--selftest` 会确认空实现能编译、基线能通过 `make check`、`make tsan` 和 `make score`（需要几分钟）。

打包时会在本机把基线和强参考在公开负载上各跑一遍（1、4 线程），把结果写进 `data/anchors.json`，供 Agent 用 `make score` 估分。这一步需要几分钟，大部分是基线；**请在运行 Agent 的同一台机器上打包**，否则估分会有偏差。没有强参考（私有子模块）时加 `--anchors none`，工作区里的 `make score` 会提示不可用。

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
- 每到快照点（30、60、90 分钟），以及收卷前 10 分钟和 2 分钟（第 95、103 分钟），运行器会把“现在几点、还剩几分钟”插进正在进行的会话；第 90 分钟的那条同时提醒“推荐用时已到”，之后只做修错和小改进；
- Agent 随时可以运行 `make time`，`make check` 和 `make bench` 的最后一行也会显示剩余时间。

**交卷与收卷**：
- Agent 运行 `make submit` 即提前交卷，几秒内结束；
- 到点时先中断会话、杀掉 Agent 的全部进程，再拍最后一张快照，之后 `src/` 不会再变；
- Agent 没交卷就停下时，会被告知剩余时间并继续（最多 `--max-resumes` 次，连续 3 次一续跑就立刻停下则判为卡死并结束）。

结束后 `<out>/run.json` 记录结束原因、用时、续跑次数、token 和费用、时间线事件、任务版本和机器信息；`<out>/logs/messages.json` 是完整会话，`<out>/logs/transcript.json` 是转换后的统一格式。费用来自 OpenCode 的模型价格数据，自定义提供商如果没有配置价格会显示为 0。

## 3b. 人工模式：用其他 Agent 工具

适用于 Claude Code、Codex CLI、Cursor、Aider 等运行器驱动不了的工具。运行器负责规则，人只负责原样转发文本：

```sh
python3 tasks/railway/agent/run_manual.py run --workspace ~/llmbench/workspace --out ~/llmbench/runs/cc1 \
    --tool claude-code --tool-version 2.1.0 --model anthropic/claude-x --variant high
```

1. 运行器准备好工作区后，在工作区里启动你的工具（权限设为自动批准，关掉联网搜索和网页抓取），回到运行器终端按 Enter 开始计时。
2. 运行器打印带开始和收卷时间的提示词，同时写入 `<out>/prompt.txt`；装了 `wl-copy`、`xclip`、`xsel` 或 `pbcopy` 时会直接放进剪贴板。把它作为第一条消息原样粘贴。
3. 运行中**只能粘贴运行器给出的文本**，不能做任何额外指导：
   - 时间提醒：到点时运行器响铃并打印（同时复制），原样粘贴；
   - 续跑：Agent 没交卷就停下时，在运行器终端输入 `r` 回车，粘贴它给出的续跑语（最多 20 次）；
   - 其他按键：`n` 重新显示上一条提醒，`p` 重新显示提示词，`s` 查看状态。
4. Agent 运行 `make submit` 即交卷；到点时运行器立即拍最终快照，这就是成绩，之后工具再改什么都不算。正式模式下会杀掉 `llmagent` 的全部进程；试跑模式下可以用 `--on-deadline '命令'` 让运行器帮你关掉工具。最后 `src/` 会被设为只读。
5. 结束时运行器询问会话记录和用量（都可以跳过）。会话记录格式目前支持 `claude-code`（`~/.claude/projects/<项目>/<会话>.jsonl`）和 `opencode`。事后也可以补：

```sh
python3 tasks/railway/agent/run_manual.py attach --out ~/llmbench/runs/cc1 \
    --transcript claude-code ~/.claude/projects/<项目>/<会话>.jsonl
python3 tasks/railway/agent/run_manual.py attach --out ~/llmbench/runs/cc1 --cost 4.2 --input-tokens 1200000 --output-tokens 80000
```

`--tool` 要用规范名称（`claude-code`、`codex`、`gemini-cli`、`qwen-code`、`kimi-cli`、`cursor`、`windsurf`、`trae`、`copilot`、`cline`、`roo-code`、`kilo-code`、`aider`、`amp`、`goose`、`crush`、`opencode`），同一个工具才不会被分成几组；确实是新工具时加 `--tool-unlisted`。需要别的工具的会话导入时，把它的一份日志样本发给维护者，补一个 `transcript.py` 转换器即可。

**正式模式**（`--strict`，需要 root）：先用 `vm_netguard.sh enable llmagent <该工具的 API 域名>` 限制网络，并在 `llmagent` 账户下登录好工具；运行器会打印以 `llmagent` 身份启动工具的命令，例如 `sudo -u llmagent -H bash -c 'cd <工作区> && claude'`。

## 4. 评分

```sh
E="python3 tasks/railway/eval/railway_eval.py"
$E prepare   --season seasons/s1 --configs private/railway/hidden --seed <赛季种子>   # 每季一次，较慢
$E snapshots --season seasons/s1 --snapshots ~/llmbench/runs/run1/snapshots --out ~/llmbench/runs/run1/results
```

`results/curve.json` 是分数随时间变化的曲线；最终成绩在最后一张快照的结果里。

## 5. 生成报告

```sh
python3 tasks/railway/report/report.py --season seasons/s1 --runs ~/llmbench/runs --out ~/llmbench/report \
    --env-file ~/keys.env [--names names.json] [--full-transcript]
```

用浏览器打开 `~/llmbench/report/index.html`。整个目录不依赖网络，可以直接打包发给别人或放到任意静态网站上。

- `--runs` 可以给多个运行目录，也可以给一个父目录（自动找两层以内带 `run.json` 的目录）。还没评分的运行会列为“未评分”，并打印评分命令。
- 首页是排行榜：按“模型 + variant + 工具”分组，只统计**标准运行**——正式模式、时限 105 分钟、用本赛季评分、任务包和提示词版本与赛季一致。其他运行有详情页，但列在“其他运行”里，并写明原因。
- 每次运行一个详情页：得分曲线、Agent 自测的 `make check` 结果、9 组测量和锚点对比、延迟分布和压测期间的吞吐、正确性和偏好、Agent 活动时间线、上下文和费用、代码演变和最终源码、会话记录。
- `--env-file` 里的每个值、运行的网页密码、形如常见 API 密钥的字符串都会被替换成 `***`。会话记录里的工具输出默认只保留前后各 20 行，`--full-transcript` 保留全部。
- `--names` 是一个 JSON 文件，把模型 ID 映射成显示名，例如 `{"anthropic/claude-x": "Claude X"}`。

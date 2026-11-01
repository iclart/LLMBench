# LLMBench 铁路任务：版本与操作口径

目前仓库有两套**互不兼容**的接口和评测流程：ABI 1（现有正式流程）和 ABI 2（实验流程，未发布正式赛季）。不要在两者间混用工作区、赛季数据、评分锚点或排行榜。

| 项目 | ABI 1 | ABI 2 |
|---|---|---|
| 规格 | [铁路任务](../tasks/railway/TASK.md) | [实验任务](../tasks/railway/v2/TASK.md) |
| 接口 | `tasks/railway/include/ticket_system.h` | `tasks/railway/v2/include/ticket_system_v2.h` |
| Agent 指南 | [agent/README.md](../tasks/railway/agent/README.md) | [v2/README.md](../tasks/railway/v2/README.md) |
| 打包 | `tasks/railway/agent/package_workspace.py` | `tasks/railway/v2/package_workspace.py` |
| 评测 | `tasks/railway/eval/railway_eval.py` | `tasks/railway/v2/evaluate.py` |
| 子命令 | `prepare`, `evaluate`, `snapshots` | `prepare`, `calibrate`, `evaluate`, `score` |
| 评分 | `tasks/railway/eval/scoring.py` | `tasks/railway/v2/tools/scoring.py` |
| 报告 | `tasks/railway/report/report.py` | `tasks/railway/v2/report.py` |

## 核实运行入口

从仓库根目录开始，先构建并测试：

```sh
cmake -S . -B build
cmake --build build -j
ctest --test-dir build --output-on-failure
```

ABI 1 使用以下帮助和指南确认参数：

```sh
python3 tasks/railway/eval/railway_eval.py --help
python3 tasks/railway/agent/package_workspace.py --help
```

ABI 2 使用独立 CLI：

```sh
python3 tasks/railway/v2/evaluate.py --help
python3 tasks/railway/v2/validate.py --help
python3 tasks/railway/v2/package_workspace.py --help
```

ABI 2 的 `prepare` 要求 `--season` 和 `--build`；公开实验加 `--public`，隐藏输入使用 `--world`。独立运行 `calibrate` 测量压测器内存开销，`evaluate` 通过 `--engine` 或 `--src` 选择被测实现；`score` 使用同 ABI 2 的 measured、baseline 和 strong 结果。完整流程见 [v2 指南](../tasks/railway/v2/README.md)。

## 成绩和环境边界

- ABI 1 的 `make score` 是公开负载本机估分；正式评测使用隐藏赛季、独立裁判与正式 Linux 隔离。
- ABI 2 的 Windows 运行只用于试跑。当前实验测量不应声称等价于正式 Linux `bwrap` 隔离。
- ABI 2 的 `published=false`、`total=null` 表示尚未满足正式发布条件；不要把实验报告并入 ABI 1 排行榜。
- ABI 2 的 `task_version()` 哈希本目录中 `.py`、`.cpp`、`.h`、`.md` 文件及 ABI 1 接口头文件。修改 v2 文档可能使已有赛季指纹失效；需要重新 `prepare` 和校准锚点。

## 文档维护原则

接口合同以对应 `TASK.md` 和头文件为准；CLI 参数以源代码和 `--help` 为准；评分细节以各自的 `scoring.py` 为准。设计文档负责解释实现，不应覆盖实际规则。发现契约和代码矛盾时标记差异并修复，不要靠模糊措辞抹平。未经实际执行的构建、测试与隔离检查不应标记为通过。

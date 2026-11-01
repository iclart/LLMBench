# 铁路任务版本与结果口径

**默认主线：ABI 2。** [ABI 2 题目](../tasks/railway/v2/TASK.md) 是当前正式公开的任务规格；[ABI 1](../tasks/railway/TASK.md) 是保留原有输入、工具和结果的历史版本。版本提升不等于对任何未完成测量或赛季发布成绩背书。

| 项目 | ABI 2（当前主线） | ABI 1（历史保留） |
|---|---|---|
| 规格与接口 | [v2/TASK.md](../tasks/railway/v2/TASK.md)、`v2/include/ticket_system_v2.h` | [TASK.md](../tasks/railway/TASK.md)、`include/ticket_system.h` |
| 工作区打包 | `tasks/railway/v2/package_workspace.py` | `tasks/railway/agent/package_workspace.py` |
| 使用指南 | [v2/README.md](../tasks/railway/v2/README.md) | [旧版 Agent 指南](../tasks/railway/agent/README.md) |
| 评测 CLI | `tasks/railway/v2/evaluate.py` | `tasks/railway/eval/railway_eval.py` |
| 子命令 | `prepare`、`calibrate`、`evaluate`、`score` | `prepare`、`evaluate`、`snapshots` |
| 评分代码 | `v2/tools/scoring.py` | `eval/scoring.py` |
| 报告 | `v2/report.py` | `report/report.py` |
| 赛季与锚点 | 独立的 ABI 2 基线 / 强参考 / 内存校准 | ABI 1 旧赛季与锚点，禁止跨版本混用 |

## 主线构建、评测与凭证

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
python3 tasks/railway/v2/evaluate.py --help
python3 tasks/railway/v2/validate.py --help
python3 tasks/railway/v2/package_workspace.py --help
```

完整的 `prepare --public`、`calibrate`、`evaluate`、`score` 和打包命令见 [主线指南](../tasks/railway/v2/README.md)。只有在同一任务指纹、输入哈希、平台、CPU 和编译参数下生成的锚点才能评分；无私有强参考时无法形成正式对标成绩。

**正式题目与正式成绩是两个不同状态**：ABI 2 现为主线，但评分输出必须拥有所有五个分项及发布证据才会输出数值 `total`。未达到条件时使用 `null` 和 `published=false`，而不是补零、沿用 ABI 1 分数或误称已发布。

当前 ABI 2 在 Windows 上只作本地试跑；Linux 的实验测量尚未实现与历史 ABI 1 正式评测等价的 `bwrap` 隔离。不能因选择默认主线而自动绕开平台、Sanitizer、经营区分度或同款模型试跑关卡。

## 复现与文档维护

`tasks/railway/v2/evaluate.py::task_version()` 包含 v2 目录内 `.py`、`.cpp`、`.h`、`.md` 和旧版公共接口头文件的内容；修改 v2 文档后，旧赛季、校准和锚点可能失效，请先重新 `prepare`。

任务契约以对应版本的 `TASK.md` 和接口为准；命令细节以 CLI `--help` 与源码为准；计分以对应 `scoring.py` 为准。不要改写已冻结的 ABI 1 历史分数，也不要将未执行的检查报告成已通过。

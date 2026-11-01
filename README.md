# LLMBench

评测 LLM Coding Agent 的限时工程能力：选手按固定 C++20 接口实现程序，由独立负载、正确性裁判和性能测量工具检验。

## 当前正式题目：铁路订票系统 ABI 2

**ABI 2 是当前主线题目与默认开发目标。** 规格见 [任务说明](tasks/railway/v2/TASK.md)、[接口头文件](tasks/railway/v2/include/ticket_system_v2.h)，运行和评测说明见 [ABI 2 指南](tasks/railway/v2/README.md)。

ABI 2 支持实时库存查询、区间票、原子改签、按票格部分退票、多线程并发与经营模式。它有独立的工作区打包、数据生成、裁判、评分和报告工具。

**主线正式 ≠ 某次成绩已获发布资格。** ABI 2 的评分器只有在正确性、ASan/UBSan、TSan、跨平台一致性、经营证据、难度试跑及五个分项均满足要求时才生成正式总分；否则返回 `total: null`、`published: false`。现阶段不可把试跑或未验证的结果当作官方排行榜成绩。

### 构建及基础测试

需要 CMake ≥ 3.20、C++20 编译器和 Python 3（完整运行环境见 [ABI 2 指南](tasks/railway/v2/README.md)）。

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

### ABI 2 公共工作区与试跑

以下命令在仓库根目录执行，所有输出目录必须是尚不存在的新目录；`--build` 指向 `build/tasks/railway`：

```sh
python3 tasks/railway/v2/evaluate.py prepare --season seasons/v2-public --build build/tasks/railway --public --count 100000
python3 tasks/railway/v2/evaluate.py calibrate --season seasons/v2-public --build build/tasks/railway --out results/v2-memory
python3 tasks/railway/v2/evaluate.py evaluate --season seasons/v2-public --build build/tasks/railway --engine baseline --calibration results/v2-memory/calibration.json --out results/v2-baseline
python3 tasks/railway/v2/package_workspace.py --season seasons/v2-public --build build/tasks/railway --out workspaces/v2-public --selftest
```

`--engine strong` 仅在配置 `-DRAIL_V2_STRONG=<私有强参考源码>` 后才构建；没有私有强参考时仍可运行公开构建、校验和基线试跑，但**不能形成正式对标分数**。正式评测还须用同一 ABI 2 赛季、平台、编译参数和 CPU 配置的锚点。

### 隔离与发布

- Windows MinGW 为本地功能和性能试跑；当前 ABI 2 的 Linux 测量器也**不是**正式隔离沙箱，不能宣称已满足隔离上线条件。
- 独立 `validate.py` 可采集测试和跨平台生成证据；`tools/scoring.py` 的发布关卡不因“主线”身份而自动放行。
- ABI 2 的任务指纹包含其目录下的 Markdown；修改文档后须重新准备赛季及校准数据。

## 历史版：ABI 1

ABI 1 保留在 [`tasks/railway/TASK.md`](tasks/railway/TASK.md) 和 [旧 Agent 指南](tasks/railway/agent/README.md) 中，旧赛季评测入口为 `tasks/railway/eval/railway_eval.py`。旧接口、成绩和参考锚点**不与 ABI 2 互通**。设计沿革见 [ABI 1 设计文档](docs/DESIGN.md)。

完整的版本、命令与结果边界见 [`docs/VERSIONS.md`](docs/VERSIONS.md)。

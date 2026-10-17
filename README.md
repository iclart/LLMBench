# LLMBench

一个评测 LLM Agent 工程能力的框架：给 Agent 一份规格和固定的工具链，限时从零写出一个程序，再用独立的压测器和裁判打分。

设计决策与理由见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 题目

| 题目 | 目录 | 状态 |
|---|---|---|
| 铁路订票系统：高并发、区间票、换乘规划、选座 | [`tasks/railway/`](tasks/railway/) | 里程碑 1：规格与数据 |

## 铁路订票系统

- [`TASK.md`](tasks/railway/TASK.md)：交给 Agent 的完整规格。
- [`include/ticket_system.h`](tasks/railway/include/ticket_system.h)：选手要实现的 C++ 接口。
- `tools/`：负载生成器 `railgen` 与共用代码；压测器和裁判在后续里程碑加入。
- `configs/public/`：公开负载配置（规模约为隐藏负载的 1/10）。隐藏配置放在被 `.gitignore` 的 `configs/hidden/`，不入库。

### 构建与测试

需要 CMake ≥ 3.20 和支持 C++20 的 g++（Linux）或 MinGW-w64 g++（Windows）。

```sh
cmake -S . -B build
cmake --build build -j
ctest --test-dir build --output-on-failure
```

### 生成数据

```sh
G=build/tasks/railway/railgen
C=tasks/railway/configs/public
$G world $C/world.cfg 1 pub.world
$G load  pub.world $C/daily.cfg 2 daily.load      # 另有 spring.cfg、refund_wave.cfg
$G stats pub.world daily.load
```

同一个配置和种子在 Linux 和 Windows 上生成逐字节相同的文件，由测试里的哈希检查保证。

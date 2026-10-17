# LLMBench

一个评测 LLM Agent 工程能力的框架：给 Agent 一份规格和固定的工具链，限时从零写出一个程序，再用独立的压测器和裁判打分。

设计决策与理由见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 题目

| 题目 | 目录 | 状态 |
|---|---|---|
| 铁路订票系统：高并发、区间票、换乘规划、选座 | [`tasks/railway/`](tasks/railway/) | 里程碑 3：并发压测器与裁判 |

## 铁路订票系统

- [`TASK.md`](tasks/railway/TASK.md)：交给 Agent 的完整规格。
- [`include/ticket_system.h`](tasks/railway/include/ticket_system.h)：选手要实现的 C++ 接口。
- `tools/`：负载生成器 `railgen`、单线程驱动 `railrun`、并发压测器 `railbench`、裁判 `railjudge` 与共用代码。
- `tests/mutants/`：注入典型错误的变异引擎，裁判必须全部抓到。
- `reference/baseline/`：基线参考实现（性能分 0 分锚点）。
- `reference/oracle/`：穷举式判定基准，只用于交叉比对 `query`。
- `configs/public/`：公开负载配置（规模约为隐藏负载的 1/10）。
- 强参考实现和隐藏配置在私有子模块 `private/`（[iclart/LLMBenchReference](https://github.com/iclart/LLMBenchReference)）中，没有权限时其余部分照常构建。

需要强参考时：`git submodule update --init private`。

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

R=build/tasks/railway/railrun_baseline   # 或 railrun_oracle、railrun_strong
$R verify pub.world daily.load --limit 2000          # 单线程重放并校验
$R query  pub.world daily.load out.answers --random 1000 7

# 并发压测 + 判定：参考答案来自任何与 oracle 一致的引擎
build/tasks/railway/railrun_baseline query pub.world daily.load ref.answers
build/tasks/railway/railbench_baseline pub.world daily.load 4 run.log
build/tasks/railway/railjudge pub.world daily.load ref.answers run.log   # 输出 JSON，出错时退出码为 1
```

ThreadSanitizer 构建：`cmake -S . -B build-tsan -DRAIL_TSAN=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo`。

同一个配置和种子在 Linux 和 Windows 上生成逐字节相同的文件，由测试里的哈希检查保证。

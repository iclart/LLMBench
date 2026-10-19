# 铁路题实验赛季 ABI 2

本目录实现实时余票查询、任意票格退款、原子改签及分模式经营回放。现版 ABI 1、旧报告和正式锚点独立保留。私有强参考通过 `RAIL_V2_STRONG` 指定，禁止复制进公开包。

## 构建与检查

在仓库根目录用 CMake 构建后运行 `ctest --test-dir <build> --output-on-failure`。Windows 使用 MinGW POSIX、C++20、`-O2 -DNDEBUG`。Windows 的 `GetLogicalProcessorInformationEx` 选择四个不同物理核，Linux 使用 sysfs 的核心拓扑；线程数固定为 1、4、16。

Windows 压测器使用 QPC 的纳秒单位时间戳。MinGW 的 TLS 析构缺陷可用 `../tests/windows_tls_probe.cpp` 复现。Windows 工作线程完成后等待进程退出，主线程确认所有调用结束、关闭日志后调用 `ExitProcess`；Linux 正常 join。该措施不改变引擎接口语义，也不替代 Linux 的 TSan 检查。

Linux 分别使用 `-DRAIL_ASAN_UBSAN=ON` 与 `-DRAIL_TSAN=ON` 创建独立构建目录。`validate.py --build <build> --out <evidence>` 运行真实 CTest 并记录生成器哈希；另一平台用 `--compare <evidence>/validation.json` 对照。只在两个不同操作系统上的任务指纹和世界/负载哈希均相同时记录跨平台一致性通过。打包时可用 `--tsan-build <Linux-tsan-build/tasks/railway>` 提供 `make tsan` 所需静态库。

`reference/baseline.cpp` 使用全局锁；`tools/model.py` 是按实际返回座位更新的独立顺序模型。最多八次调用的重叠历史穷举全部合法线性化顺序；大历史使用保守库存包络、订单守恒与静默阶段精确检查。结果中的 `unresolved_calls` 明确保留未证明线性化的调用数。不把保守审计通过写成完整线性化证明。

七个 C++ 变异分别覆盖漏方案、退款多释放、改签半提交、失败订票泄漏、重复退票成功、查询忽略库存、退票不释放；混合快照由独立历史测试覆盖。跨日、碎片、旧票复用、回滚、交叠/不交叠票格操作均有功能检查。

## 可复现的实验流水线

以下 `<build>` 指 `build/tasks/railway`，各输出目录必须全新。最终修改源码后再生成赛季；任务指纹变化会拒绝复用旧赛季和锚点。

```powershell
python tasks/railway/v2/evaluate.py prepare --season <public-season> --build <build> --public --count 100000
python tasks/railway/v2/evaluate.py calibrate --season <public-season> --build <build> --out <memory-calibration>
python tasks/railway/v2/evaluate.py evaluate --season <public-season> --build <build> --engine baseline --calibration <memory-calibration>/calibration.json --out <baseline-results>
python tasks/railway/v2/evaluate.py evaluate --season <public-season> --build <build> --engine strong --calibration <memory-calibration>/calibration.json --out <strong-results>
python tasks/railway/v2/package_workspace.py --season <public-season> --build <build> --out <workspace> --selftest
python tasks/railway/v2/pilots.py --workspace <workspace> --out <pilot-preflight>
```

隐藏赛季使用 `prepare --world <private-world>`。评测选手使用 `evaluate --src <src-dir>`；评分使用 `evaluate score --measured <result.json> --baseline <result.json> --strong <result.json> --out <score.json>`。锚点必须具有相同输入哈希、任务指纹、平台、编译参数及核心配置，ABI 1 锚点会被拒绝。每接口不足一万次测量时性能分缺失，不能用零样本生成满分。内存先测相同负载的空引擎开销，再从峰值中扣除；Windows Job Object 限制提交内存并在超时杀掉进程树。Windows 为本地试跑，当前 Linux runner 不是正式隔离沙箱。

公开包按白名单复制头文件、空实现、负载、编译好的测试/驱动静态库及 Python 字节码。顺序裁判字节码可被选手分析，不承载秘密；私有引擎和隐藏参数不得进入包。Python 主次版本必须匹配。包内 `make check/bench/score/time/submit` 可用；实验版 `score` 保留正式总分为空。

## 经营与发布关卡

`tools/management.py --policy <private-policy.py> --out <study.json>` 使用固定三类场景、10 个训练种子与 30 个独立验证种子。策略只看到训练混合分布、当前请求、过去需求、当前库存及公开 λ。简单策略和高级策略参数均只用训练集选择；固定区间离线最优使用最小费用流，并与小规模穷举交叉验证。报告保留 P、N、效用、配对置信区间和胜率。`export_episode` 可导出真实世界/固定行程负载，不把 Python 模拟器结果冒充 C++ 引擎成绩。

至少两类场景满足收益、稳定性和三档取舍后，才可校准经营分。研究政策尚未与 C++ 回放逐项核对时也不能启用。两轮研究失败结果均保留，不修改阈值凑分。

`pilots.py --execute` 顺序创建三个空实现工作区，调用同款模型 `opencode-go/deepseek-v4.1-flash#max`、OpenCode 2.0.32、105 分钟的试跑器；工具版本不同会记录差异并停止，不伪装成同款验证。随后用同一隐藏赛季评测每个最终 `src/`，使用 `tools/scoring.py::pilot_gate` 验证至少两次正确、中位性能分不超过 80。

正式总分必须有 65/10/12/8/5 五个组件，并通过正确性、ASan/UBSan、TSan、跨平台生成一致性、经营区分度和三次难度试跑。当前没有这些证据时，`total=null`、`published=false`。`report.py` 生成独立实验 HTML，不改旧榜单。

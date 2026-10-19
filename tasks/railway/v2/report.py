#!/usr/bin/env python3
"""Self-contained ABI 2 research report. ABI 1 HTML and scores are untouched."""
import argparse
import html
import json
from pathlib import Path


def table(headers, rows):
    cell = lambda value: html.escape(str(value))
    return "<table><thead><tr>" + "".join("<th>"+cell(h)+"</th>" for h in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join("<td>"+cell(v)+"</td>" for v in row) + "</tr>" for row in rows) + "</tbody></table>"


def render(comparison=None, studies=(), evaluation=None, validation=None, pilots=None, comparisons=()):
    content = ["<h1>铁路题升级 · 实验报告</h1>", "<p class=notice>实验结果不替换现版锚点，不与历史 Linux 成绩混榜。未通过发布关卡时不显示正式总分。</p>"]
    for comparison in ([comparison] if comparison else [])+list(comparisons):
        gate = comparison["gate"]
        content += ["<h2>现版强参考 · "+html.escape(comparison.get("label","候选"))+"</h2>", "<p>平台："+html.escape(gate["platform"])+"；验收："+("通过" if gate["passed"] else "未通过")+"</p>"]
        content.append(table(("指标", "候选 / 旧参考"), (("吞吐几何平均",f'{gate["throughput_ratio"]:.3f}'),("订票 p99 几何平均",f'{gate["book_p99_ratio"]:.3f}'))))
        content.append(table(("测量", "吞吐比", "查询 p99 比", "订票 p99 比"),
                             ((key, f'{v["candidate"]["throughput"]/v["strong"]["throughput"]:.3f}',
                               f'{v["candidate"]["query_p99_ns"]/v["strong"]["query_p99_ns"]:.3f}',
                               f'{v["candidate"]["book_p99_ns"]/v["strong"]["book_p99_ns"]:.3f}') for key,v in comparison["measurements"].items())))
        content.append("<p>"+html.escape("；".join(gate["failures"]))+"</p>")
    for study in studies:
        content.append("<h2>经营场景研究 · 修订 "+str(study.get("study_revision",1))+"</h2>")
        content.append("<p>独立验证关卡："+("通过" if study["gate_passed"] else "未通过，经营评分未启用")+"。离线最优知道未来，只作为上界。</p>")
        content.append(table(("场景", "服务档位", "前沿有不同取舍", "通过"),
                             ((name, ", ".join(str(e["lambda"]) for e in family["entries"]), family["distinct_tradeoffs"], family["passed"])
                              for name,family in study["families"].items())))
        rows = []
        for name,family in study["families"].items():
            for entry in family["entries"]:
                for strategy in ("greedy", "simple", "online", "offline"):
                    values = [row[strategy] for row in entry["rows"]]
                    rows.append((name,entry["lambda"],strategy,
                                 round(sum(v["people"] for v in values)/len(values),2),
                                 round(sum(v["transport"] for v in values)/len(values),2),
                                 round(sum(v["utility"] for v in values)/len(values),2)))
        content.append("<details><summary>各服务档位的人数、旅客分钟与效用</summary>"+table(("场景","λ","策略","人数","旅客分钟","效用"),rows)+"</details>")
        content.append(table(("场景","λ","比来者即售","比简单策略","配对 CI 下界"),
                             ((name,e["lambda"],f'{e["gain_greedy"]:+.1%}',f'{e["gain_simple"]:+.1%}',f'{e["paired_ci_lower"]:+.1%}')
                              for name,family in study["families"].items() for e in family["entries"])))
    if evaluation:
        content.append("<h2>新版实现验证</h2>")
        content.append("<p>任务指纹："+html.escape(evaluation["task_version"])+"；状态：实验版。正式总分：未启用。</p>")
        content.append(table(("测量","正确性检查","吞吐 /s","查询 p99 ns","订票 p99 ns","改签 p99 ns","退票 p99 ns"),
                             ((key,value["ok"],round(value.get("throughput",0)),*[value.get("latency",{}).get(op,{}).get("p99_ns","–") for op in ("query","book","exchange","refund")])
                              for key,value in evaluation["measurements"].items())))
        content.append("<p>小规模历史做完整线性化检查；大规模历史采用保守审计。通过大规模检查不等于证明每次实时查询都可线性化。</p>")
        content.append("<p>未完成的发布关卡："+html.escape("、".join(evaluation.get("release",{}).get("missing",[])))+"</p>")
    if validation:
        content.append("<h2>功能与平台验证</h2>")
        content.append(table(("平台","CTest","ASan / UBSan","TSan","跨平台生成一致性"),
                             [(validation["platform"],validation["tests_passed"],validation["asan_ubsan"],validation["tsan"],validation["cross_platform_generator"])]))
    if pilots:
        content.append("<h2>同款模型试跑</h2>")
        content.append("<p>预检状态："+html.escape(pilots["status"])+"；目标 OpenCode "+html.escape(pilots["expected"]["tool_version"])+
                       "，本机 "+html.escape(pilots.get("actual_tool_version","不可用"))+"。已启动试跑 "+str(len(pilots["trials"]))+" 次。</p>")
        if pilots.get("reason"):content.append("<pre>"+html.escape(pilots["reason"])+"</pre>")
    return "<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>铁路题升级 · 实验报告</title><style>body{font:15px/1.65 system-ui,sans-serif;color:#202124;background:#fafaf8;max-width:1120px;margin:32px auto;padding:0 20px 60px}h1{font-size:28px}h2{margin-top:36px;font-size:21px}.notice{padding:16px;background:#fff1cf;border-radius:8px}table{border-collapse:collapse;width:100%;margin:14px 0 24px;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:9px 12px;border-bottom:1px solid #ddd}td:first-child,th:first-child{text-align:left}th{background:#efefeb}details{overflow:auto}summary{cursor:pointer}</style>" + "".join(content) + "</html>"


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--comparison",type=Path,action="append",default=[])
    p.add_argument("--management",type=Path,action="append",default=[])
    p.add_argument("--evaluation",type=Path)
    p.add_argument("--validation",type=Path)
    p.add_argument("--pilots",type=Path)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    read=lambda path:json.loads(path.read_text()) if path else None
    a.out.parent.mkdir(parents=True,exist_ok=True)
    comparisons=[]
    for path in a.comparison:
        value=read(path);value["label"]=path.parent.name;comparisons.append(value)
    a.out.write_text(render(studies=[read(path) for path in a.management],evaluation=read(a.evaluation),validation=read(a.validation),pilots=read(a.pilots),comparisons=comparisons),encoding="utf-8")
    print(a.out)


if __name__=="__main__":
    main()

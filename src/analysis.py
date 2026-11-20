"""计算课程实验的固定指标，生成可复核的 CSV、Markdown 和 SVG。"""

import csv
import io
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile


STEPS = ("S1", "S2", "S3", "S4")
FAULTS = ("F2", "F3")
TRUTH = {"F0": "OK", "F2": "S2", "F3": "S3"}
LABELS = {"OK", "S2", "S3", "UNKNOWN"}
VIEWS = ("V0", "V1", "V2")


def read_rows(path):
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def save_text(path, content, encoding="utf-8"):
    # 同目录临时文件保证读者看到的是一份完整的表。
    with NamedTemporaryFile("w", encoding=encoding, newline="", dir=path.parent,
                            delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_csv(path, rows):
    table = io.StringIO(newline="")
    writer = csv.DictWriter(table, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    save_text(path, table.getvalue(), "utf-8-sig")


def percentage(part, whole):
    return round(100 * part / whole, 2) if whole else None


def ratio(part, whole):
    return f"{part}/{whole}（{part / whole:.0%}）" if whole else "—"


def supported(label, evidence, payload):
    """标签、字段覆盖、值与方向都必须能由当时可见的输入支持。"""
    months = {"intent_month", "out_month", "db_month"}
    if not isinstance(evidence, dict) or not months.issubset(evidence):
        return False
    for field, value in evidence.items():
        if field not in payload or type(value) is not type(payload[field]) or value != payload[field]:
            return False
    intent, out, db = (payload[k] for k in ("intent_month", "out_month", "db_month"))
    if label == "S2":
        return intent != out and db == out
    if label == "S3":
        return intent == out and db != out
    return label == "OK" and intent == out == db


def diagnostic_cases(method, predictions, keys, payloads):
    cases = []
    for item in keys:
        uid = item["id"]
        prediction = predictions.get(uid)
        label = prediction.get("label") if prediction else None
        if label not in LABELS:
            outcome = "missing" if prediction is None else "invalid"
        elif label == "UNKNOWN":
            outcome = "abstained"
        elif label == TRUTH[item["fault"]]:
            outcome = "correct"
        elif label == "OK":
            outcome = "wrong_clear"
        else:
            outcome = "wrong_step"
        proof = prediction.get("evidence", {}) if prediction else {}
        cases.append({
            "method": method, "id": uid, "task_id": item["task_id"], "run_id": item["run_id"],
            "view": item["view"], "fault": item["fault"], "label": label,
            "outcome": outcome, "grounded_hit": outcome == "correct" and supported(label, proof, payloads[uid]),
            "evidence": json.dumps(proof, ensure_ascii=False, sort_keys=True),
        })
    return cases


def localization_table(cases):
    grouped = defaultdict(list)
    for row in cases:
        subset = "injected" if row["fault"] in FAULTS else "clean"
        grouped[row["method"], row["view"], subset].append(row)
    summary = []
    for (method, view, subset), group in grouped.items():
        counts = Counter(r["outcome"] for r in group)
        decided = counts["correct"] + counts["wrong_step"] + counts["wrong_clear"]
        summary.append({
            "method": method, "view": view, "subset": subset, "planned": len(group),
            "correct": counts["correct"], "accuracy_pct": percentage(counts["correct"], len(group)),
            "grounded_hits": sum(r["grounded_hit"] for r in group), "wrong_step": counts["wrong_step"],
            "wrong_clear": counts["wrong_clear"], "abstained": counts["abstained"],
            "invalid": counts["invalid"], "missing": counts["missing"], "decided": decided,
            "coverage_pct": percentage(decided, len(group)),
        })
    return sorted(summary, key=lambda r: (r["method"], r["view"], r["subset"]))


def quantile(sorted_values, p):
    index = (len(sorted_values) - 1) * p
    low, high = math.floor(index), math.ceil(index)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (index - low)


def paired_gain(method, cases):
    # 每个基础任务只有一个配对差值。模型对某条记录弃答计为未命中。
    per_task = defaultdict(dict)
    for row in cases:
        if row["fault"] in FAULTS and row["view"] in ("V1", "V2"):
            per_task[row["task_id"]][row["view"], row["fault"]] = row["outcome"]
    effects = []
    for outcomes in per_task.values():
        if len(outcomes) < 4 or "missing" in outcomes.values() or "invalid" in outcomes.values():
            continue
        hits_v1 = sum(outcomes["V1", f] == "correct" for f in FAULTS)
        hits_v2 = sum(outcomes["V2", f] == "correct" for f in FAULTS)
        effects.append((hits_v2 - hits_v1) * 50)
    result = {"method": method, "planned_tasks": len(per_task), "complete_tasks": len(effects),
              "gain_pp": None, "ci_2_5": None, "ci_97_5": None, "bootstrap_draws": 0}
    if effects and len(effects) == len(per_task):
        rng = random.Random(2025)
        draws = sorted(sum(rng.choices(effects, k=len(effects))) / len(effects) for _ in range(2000))
        result.update(gain_pp=round(sum(effects) / len(effects), 2), ci_2_5=round(quantile(draws, .025), 2),
                      ci_97_5=round(quantile(draws, .975), 2), bootstrap_draws=len(draws))
    return result


def rescue_analysis(traces, replays):
    index = {}
    for row in replays:
        key = (row["task_id"], row["fault"], row["step"], row["arm"])
        if key in index:
            raise ValueError(f"确定性续跑存在重复条件 {key}")
        index[key] = row["verdict"]
    failures = [r for r in traces if r["fault"] in FAULTS and not r["verdict"]["evidence"]]
    matrix = []
    for fault in FAULTS:
        ids = [r["task_id"] for r in failures if r["fault"] == fault]
        for step in STEPS:
            pairs = [(index.get((uid, fault, step, "original")), index.get((uid, fault, step, "replace")))
                     for uid in ids]
            complete = [(a, b) for a, b in pairs if a is not None and b is not None]
            base = sum(a["evidence"] for a, _ in complete)
            fix = sum(b["evidence"] for _, b in complete)
            matrix.append({
                "fault": fault, "step": step, "planned_pairs": len(ids), "complete_pairs": len(complete),
                "original_pass": base, "replacement_pass": fix, "gain_pp": percentage(fix - base, len(complete)),
                "effective_pairs": sum(not a["evidence"] and b["evidence"] for a, b in complete),
                "replacement_stops": sum(b["status"] == "stopped" for _, b in complete),
            })
    sets = []
    for row in failures:
        effective = []
        missing = []
        for step in STEPS:
            key = (row["task_id"], row["fault"], step)
            original, replacement = index.get((*key, "original")), index.get((*key, "replace"))
            if original is None or replacement is None:
                missing.append(step)
            elif not original["evidence"] and replacement["evidence"]:
                effective.append(step)
        sets.append({"task_id": row["task_id"], "run_id": row["id"], "fault": row["fault"],
                     "effective_steps": ",".join(effective), "n_effective": len(effective),
                     "missing_steps": ",".join(missing)})
    return matrix, sets


def acceptance_table(traces):
    grouped = defaultdict(list)
    for row in traces:
        trace = row["trace"]
        wrong = any(trace["receipt"][k] != trace["intent"][k] for k in ("month", "station"))
        grouped[row["collision"], bool(wrong)].append(row["verdict"])
    table = []
    for (collision, wrong), verdicts in sorted(grouped.items()):
        for check in ("numeric", "evidence"):
            passed = sum(v[check] for v in verdicts)
            table.append({"collision": collision, "actual_query": "wrong" if wrong else "correct",
                          "checker": check, "n": len(verdicts), "passed": passed,
                          "failed": len(verdicts) - passed, "false_accept": passed if wrong else 0,
                          "false_reject": 0 if wrong else len(verdicts) - passed})
    return table


def heatmap(matrix):
    elements = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 770 224" role="img" aria-labelledby="title">',
                '<title id="title">按故障源头和干预位置划分的补救增益，单位为百分点</title>',
                '<rect width="770" height="224" fill="white"/>',
                '<g font-family="Microsoft YaHei, sans-serif" fill="#183342" text-anchor="middle">',
                '<text x="385" y="28" font-size="19">单步补救增益（百分点）</text>']
    for i, step in enumerate(STEPS):
        elements.append(f'<text x="{200 + 155*i}" y="59">{step}</text>')
    for j, fault in enumerate(FAULTS):
        elements.append(f'<text x="62" y="{105 + j*68}">{fault} 错月</text>')
        for i, step in enumerate(STEPS):
            value = next(r["gain_pp"] for r in matrix if (r["fault"], r["step"]) == (fault, step))
            x, y = 127 + 155*i, 72 + 68*j
            # 统一用 [-100, 100] 百分点色域，避免不同运行按各自极值重新缩放。
            base = (244, 246, 246)
            end = (49, 140, 131) if value is None or value >= 0 else (198, 100, 83)
            strength = abs(value or 0) / 100
            color = tuple(round(a + (b-a)*strength) for a, b in zip(base, end))
            label = "缺失" if value is None else f"{value:+.0f}" if value else "0"
            elements.append(f'<rect x="{x}" y="{y}" width="146" height="58" rx="4" fill="rgb{color}"/>')
            elements.append(f'<text x="{x+73}" y="{y+36}" font-size="21">{label}</text>')
    elements.append('</g></svg>')
    return "\n".join(elements)


def summarize(root: Path):
    base = root / "results"
    meta = json.loads((root / "run.json").read_text(encoding="utf-8"))
    split = {"dev": "开发集", "test": "测试集"}[meta["split"]]
    traces = read_rows(base / "traces.jsonl")
    replays = read_rows(base / "replays.jsonl")
    keys = read_rows(base / "blind_key.jsonl")
    payloads = {r["id"]: r["payload"] for r in read_rows(base / "blind.jsonl")}
    methods = {"规则": read_rows(base / "rules.jsonl")}
    online = root / "online" / "judgments.jsonl"
    if online.exists() and online.stat().st_size:
        methods["真实模型"] = read_rows(online)
    cases = []
    gains = []
    for method, records in methods.items():
        # 相同工作项的重试以最新记录为准，避免重复计数。
        rows = diagnostic_cases(method, {r["id"]: r for r in records}, keys, payloads)
        cases.extend(rows)
        gains.append(paired_gain(method, rows))
    localization = localization_table(cases)
    matrix, sets = rescue_analysis(traces, replays)
    acceptance = acceptance_table(traces)
    for name, table in [("diagnostic_cases", cases), ("localization", localization), ("paired_gain", gains),
                        ("rescue_matrix", matrix), ("rescue_sets", sets), ("acceptance", acceptance)]:
        write_csv(base / f"{name}.csv", table)
    save_text(base / "rescue_heatmap.svg", heatmap(matrix))
    lines = [
        f"# {split}实验结果", "",
        f"独立基础任务 {len({r['task_id'] for r in traces})} 个，主轨迹 {len(traces)} 条，"
        f"注入后验收失败 {len(sets)} 条。生成了 {len(keys)} 份盲判输入，执行了 {len(replays)} 次确定性续跑。",
        "", "## 1. 源头定位", "",
        "准确率与覆盖率均以计划数为分母。明确给出 S2、S3 或无故障计入覆盖；"
        "有据命中要求标签正确，且所引字段真实可见，三个边界月份共同支持所指步骤。",
        "", "| 诊断者 | 视图 | 轨迹 | 准确 | 有据命中 | 错指步骤 | 错判无故障 | 弃答 | 无效/缺失 | 覆盖 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in localization:
        n = r["planned"]
        subset = "注入" if r["subset"] == "injected" else "干净"
        lines.append(f"| {r['method']} | {r['view']} | {subset} | {ratio(r['correct'], n)} | "
                     f"{r['grounded_hits']}/{n} | {r['wrong_step']} | {r['wrong_clear']} | {r['abstained']} | "
                     f"{r['invalid']}/{r['missing']} | {ratio(r['decided'], n)} |")
    lines += ["", "V0 给出请求与报告，V1 增加意图和数据库执行证据，V2 再增加工具出参月份。"]
    for r in gains:
        if r["gain_pp"] is None:
            lines += ["", f"{r['method']}的 V2−V1 对照有 {r['complete_tasks']}/{r['planned_tasks']} 个完整任务。"
                      "尚未给出全体任务的配对区间。"]
        else:
            lines += ["", f"{r['method']}在故障轨迹上，V2 相对 V1 的定位准确率差为 "
                      f"{r['gain_pp']:+g} 个百分点，按基础任务配对重采样 2,000 次的 95% 区间为 "
                      f"[{r['ci_2_5']:+g}, {r['ci_97_5']:+g}] 个百分点。"]
    if "真实模型" not in methods:
        lines += ["", "**本次尚无真实模型盲判记录。**"]
    else:
        missing = sum(r["missing"] for r in localization if r["method"] == "真实模型")
        invalid = sum(r["invalid"] for r in localization if r["method"] == "真实模型")
        if missing or invalid:
            lines += ["", f"**真实模型盲判仍有 {missing} 份缺失、{invalid} 份无效。** "
                      "表中准确率使用全部计划记录作分母。部分运行只用于检查进度，待补齐并核实异常后再比较模型效果。"]
    lines += ["", "## 2. 确定性单步补救", "",
              "| 注入位置 | 替代位置 | 原动作成功 | 替代成功 | 增益（百分点） | 替代后停止提交 |",
              "|---|---|---|---|---|---|"]
    for r in matrix:
        n = r["complete_pairs"]
        gain = "—" if r["gain_pp"] is None else f"{r['gain_pp']:+g}"
        lines.append(f"| {r['fault']} | {r['step']} | {ratio(r['original_pass'], n)} | "
                     f"{ratio(r['replacement_pass'], n)} | {gain} | {r['replacement_stops']} |")
    counts = Counter(r["n_effective"] for r in sets if not r["missing_steps"])
    n_complete = sum(counts.values())
    lines += ["", f"四个位置均已测完的轨迹有 {n_complete}/{len(sets)} 条，其中零个有效位置 {counts[0]} 条，"
              f"一个位置 {counts[1]} 条，多个位置 {ratio(n_complete - counts[0] - counts[1], n_complete)}。"
              "主动停止可以阻止错报，在统计交付的主指标中仍然算未成功。",
              "", "![补救增益](rescue_heatmap.svg)", "", "## 3. 两种验收器", "",
              "| 两月数字 | 实际查询 | 验收器 | 通过 | 拒绝 | 误放/适用样本 | 误拒/适用样本 |",
              "|---|---|---|---|---|---|---|"]
    for r in acceptance:
        wrong = r["actual_query"] == "wrong"
        lines.append(f"| {'相同' if r['collision'] else '不同'} | {'错月' if wrong else '正确'} | "
                     f"{'只看数字' if r['checker'] == 'numeric' else '含查询证据'} | "
                     f"{r['passed']} | {r['failed']} | {ratio(r['false_accept'], r['n']) if wrong else '—'} | "
                     f"{ratio(r['false_reject'], r['n']) if not wrong else '—'} |")
    lines += ["", "前后两个月计数相同的比例由实验设计指定，仅用于检验数字验收器的盲点。",
              "", "## 4. 真实执行复核", ""]
    worker = root / "online" / "worker_summary.csv"
    worker_replays = root / "online" / "worker_replays.csv"
    if worker.exists():
        lines.append("[初始运行与入选结果](../online/worker_summary.csv)")
        if worker_replays.exists():
            lines += ["", "[按任务的原动作与替代动作续跑](../online/worker_replays.csv)"]
    else:
        lines.append("本次尚无真实执行 Agent 子集的初始运行汇总。")
    lines += ["", "上述确定性枚举只覆盖已登记的四种单步干预和各步骤权限。"]
    save_text(base / "summary.md", "\n".join(lines) + "\n")

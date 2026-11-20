"""课程实验命令行。所有路径默认相对于本文件。"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sys
from tempfile import TemporaryDirectory

from src import core
from src.analysis import summarize


HERE = Path(__file__).resolve().parent
STEPS = ("S1", "S2", "S3", "S4")
FAULTS = ("F0", "F2", "F3")


def read_lines(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_lines(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def visible(trace, level):
    report = trace["report"]
    payload = {"request": trace["request"], "report": {
        k: report[k] for k in ("station", "month", "total", "bad")
    }}
    if level in ("V1", "V2"):
        receipt = trace["receipt"]
        payload["intent_month"] = trace["intent"]["month"]
        payload.update({f"db_{k}": receipt[k] for k in ("station", "month", "total", "bad")})
    if level == "V2":
        payload["out_month"] = trace["args"]["month"]
    return payload


def rule(payload):
    evidence = {}
    label = "UNKNOWN"
    if "out_month" in payload:
        intent = core.parse_request(payload["request"])
        fields = ("intent_month", "out_month", "db_month")
        evidence = {k: payload[k] for k in fields}
        if intent["month"] == evidence["intent_month"] and intent["station"] == payload["db_station"]:
            if evidence["out_month"] != intent["month"]:
                label = "S2"
            elif evidence["db_month"] != evidence["out_month"]:
                label = "S3"
            elif all(payload["report"][k] == payload["db_" + k]
                     for k in ("station", "month", "total", "bad")):
                label = "OK"
    return {"label": label, "evidence": evidence}


def opaque(seed, *parts):
    source = json.dumps([seed, *parts], ensure_ascii=False)
    return hashlib.sha256(source.encode()).hexdigest()[:24]


def evaluate(root, tasks, split, seed):
    path = root / "results"
    path.mkdir()
    traces, replays, checkpoints, checks = [], [], [], []
    blind_pairs, fault_keys = [], []
    for task in (t for t in tasks if t["split"] == split):
        grouped = {}
        for fault in FAULTS:
            run_id = opaque(seed, "run", task["id"], fault)
            trace, saved = core.simulate(root, task, fault, run_id)
            verdict = core.validate(task, trace["report"], [trace["receipt"]])
            check(verdict["evidence"] == (fault == "F0"), f"{run_id}: 意外验收结果")
            grouped[fault] = trace
            traces.append({"id": run_id, "task_id": task["id"], "fault": fault,
                           "collision": task["collision"], "trace": trace, "verdict": verdict})
            checkpoints.append({"id": run_id, "before": saved})
            fault_keys.append({"run_id": run_id, "task_id": task["id"], "fault": fault,
                               "before_month": task["month"],
                               "after_month": trace["receipt"]["month"]})
            for view in ("V0", "V1", "V2"):
                job_id = opaque(seed, "blind", run_id, view)
                blind_pairs.append(({"id": job_id, "payload": visible(trace, view)},
                                    {"id": job_id, "task_id": task["id"], "run_id": run_id,
                                     "view": view, "fault": fault}))
            if fault == "F0":
                continue
            # 恢复源头之前的同一份状态，只关闭注入；保留其余步骤。
            source_step = {"F2": "S2", "F3": "S3"}[fault]
            clean = core.replay(root, task, "F0", run_id, saved, source_step, "original")
            check(core.validate(task, clean["report"], [clean["receipt"]])["evidence"],
                  f"{run_id}: 关闭故障后没有恢复")
            for step in STEPS:
                for arm in ("original", "replace"):
                    branch = core.replay(root, task, fault, run_id, saved, step, arm)
                    score = core.validate(task, branch["report"], [branch["receipt"]])
                    if arm == "original":
                        check(score == verdict, f"{run_id} {step}: 原动作对照未复现")
                    replays.append({"id": run_id, "task_id": task["id"], "fault": fault,
                                    "step": step, "arm": arm, "verdict": score, "trace": branch})
        base, second, third = (grouped[f] for f in FAULTS)
        check(base["intent"] == second["intent"] == third["intent"], "上游任务状态出现差异")
        check(base["args"] == third["args"] and second["args"]["month"] != base["args"]["month"],
              "故障注入位置异常")
        check(second["args"]["month"] == second["receipt"]["month"] ==
              third["receipt"]["month"] == core.previous_month(task["month"]), "错月传播异常")
        for view in ("V0", "V1"):
            check(visible(second, view) == visible(third, view), f"{view} 泄漏了故障来源")
        check(set(visible(second, "V2")) - set(visible(second, "V1")) == {"out_month"},
              "V2 需要只增加一个字段")
        checks.append({"task_id": task["id"], "injection_verified": True,
                       "source_removal_recovers": True, "matched_visible_pair": True})
    random.Random(seed).shuffle(blind_pairs)
    jobs = [job for job, _ in blind_pairs]
    write_lines(path / "blind.jsonl", jobs)
    write_lines(path / "blind_key.jsonl", (key for _, key in blind_pairs))
    write_lines(path / "rules.jsonl", ({"id": j["id"], **rule(j["payload"])} for j in jobs))
    for name, rows in (("traces", traces), ("replays", replays), ("checkpoints", checkpoints),
                       ("fault_truth", fault_keys), ("construction_checks", checks)):
        write_lines(path / (name + ".jsonl"), rows)


def code_hashes():
    files = [Path(__file__).resolve(), *sorted((HERE / "src").glob("*.py"))]
    return {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def run_local(target, split, seed):
    if target.exists():
        raise ValueError(f"运行目录已存在：{target}。可用 summary 重算报表，或用 --run 指定新目录。")
    target.parent.mkdir(parents=True, exist_ok=True)
    # 暂存同盘，完整创建与校验后才公布该次运行。
    with TemporaryDirectory(prefix=".agent-lab-", dir=target.parent) as temp:
        root = Path(temp) / "run"
        root.mkdir()
        tasks = core.prepare(root, seed)
        evaluate(root, tasks, split, seed)
        write_lines(root / "results" / "task_index.jsonl", tasks)
        meta = {"seed": seed, "split": split, "python": platform.python_version(),
                "source_sha256": code_hashes(), "base_tasks": sum(t["split"] == split for t in tasks)}
        (root / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        summarize(root)
        root.rename(target)


@contextmanager
def run_lock(root):
    # 系统文件锁会在进程退出时释放，异常退出后仍可接着跑。
    path = root / ".analysis.lock"
    with path.open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("同一运行目录有另一个进程正在处理，请等它结束。") from exc
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, desc in (("local", "生成数据、确定性轨迹和结果表"), ("summary", "重新汇总本次运行"),
                       ("judge", "用真实模型独立盲判"), ("worker", "真实执行 Agent 的配对续跑"),
                       ("check", "验证模型实际调用查询工具并能基于回执提交统计")):
        cmd = sub.add_parser(name, help=desc)
        cmd.add_argument("--run", type=Path, default=HERE / "runs" / "course", help="运行目录")
        if name == "local":
            cmd.add_argument("--split", choices=("dev", "test"), default="test")
            cmd.add_argument("--seed", type=int, default=20261120)
        if name in ("judge", "worker", "check"):
            cmd.add_argument("--max-calls", type=int, required=True, help="本次允许的最大 HTTP 调用次数")
        if name == "judge":
            cmd.add_argument("--limit", type=int, help="只处理打乱队列的前 N 个任务")
    args = parser.parse_args(argv)
    root = args.run.resolve()
    if args.command == "local":
        run_local(root, args.split, args.seed)
    else:
        if not (root / "run.json").is_file():
            parser.error("运行目录不完整，请先用 local 创建。")
        with run_lock(root):
            if args.command == "summary":
                summarize(root)
                print(f"实验结果：{root / 'results' / 'summary.md'}")
                return
            from src.online import Client, check_tools, run_judges, run_workers
            if args.max_calls < 1:
                parser.error("--max-calls 必须为正整数")
            if args.command == "judge" and args.limit is not None and args.limit < 1:
                parser.error("--limit 必须为正整数")
            if args.command == "worker" and json.loads((root / "run.json").read_text(encoding="utf-8"))["split"] != "test":
                parser.error("真实执行子集使用预先登记的测试任务，请选择 test 运行目录。")
            client = Client(root, args.max_calls)
            try:
                if args.command == "judge":
                    jobs = read_lines(root / "results" / "blind.jsonl")
                    run_judges(root, jobs[:args.limit], client)
                elif args.command == "check":
                    record = check_tools(root, read_lines(root / "results" / "task_index.jsonl"), client)
                    print(f"{record['model']} 工具调用通过验收，已写入 online/connectivity.jsonl。")
                else:
                    run_workers(root, read_lines(root / "results" / "task_index.jsonl"), client)
            finally:
                summarize(root)
    print(f"实验结果：{root / 'results' / 'summary.md'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(str(error))

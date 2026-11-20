"""Synthetic station observations and the deterministic checkpoint experiment."""

from __future__ import annotations

import calendar
import copy
import csv
import hashlib
import json
import random
import re
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


DATA_VERSION = 1
STEPS = ("S1", "S2", "S3", "S4")
FAULTS = ("F0", "F2", "F3")
MONTH_NAMES = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")
RAW_FIELDS = ("task_id", "observation_id", "station", "observed_utc", "bad_flag")


def previous_month(month: str) -> str:
    first = date.fromisoformat(month + "-01")
    return (first - timedelta(days=1)).strftime("%Y-%m")


def parse_request(request: str) -> dict:
    """Parse the announced task grammar without reference to the task manifest."""
    station = re.search(r"([ABC])\s*站", request)
    when = re.search(r"(2025|二〇二五)\s*年\s*(\d{1,2}|[一二三四五六七八九十]+)\s*月", request)
    if not station or not when:
        raise ValueError(f"无法解析站点或年月: {request}")
    token = when.group(2)
    month = int(token) if token.isdecimal() else MONTH_NAMES.index(token) + 1
    return {"station": station.group(1), "month": date(2025, month, 1).strftime("%Y-%m")}


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _generate_csv(path: Path, seed: int) -> list[dict]:
    tasks = []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(RAW_FIELDS)
        for station_index, station in enumerate("ABC"):
            for m in range(2, 12):
                month = f"2025-{m:02d}"
                task_id = f"{station}-{month}"
                rng = random.Random(f"station-lab/{seed}/{task_id}")
                dev = m in (5, 11)
                collision = m in (4, 10) or (m == 5 and station in ("A", "B"))
                prompts = [
                    f"请统计 {station} 站 2025 年 {m} 月的观测条数及坏旗条数。",
                    f"{station}站二〇二五年{MONTH_NAMES[m - 1]}月一共有多少条观测，其中多少条带坏旗？",
                    f"想了解 2025 年 {m} 月 {station} 站的观测总条数和坏旗条数。",
                ]
                tasks.append(dict(id=task_id, station=station, month=month,
                                  request=prompts[(station_index + m) % len(prompts)],
                                  split="dev" if dev else "test", collision=collision))
                current = (rng.randint(60, 140), rng.randint(0, 24))
                prior = (rng.randint(60, 140), rng.randint(0, 24))
                if collision:
                    prior = current
                elif task_id == "A-2025-09":
                    current, prior = (100, 12), (80, 2)
                elif current == prior:
                    prior = (prior[0], (prior[1] + 1) % 25)

                for month_offset, (n, n_bad) in enumerate((prior, current)):
                    actual_m = m - 1 + month_offset
                    start = datetime(2025, actual_m, 1, tzinfo=timezone.utc)
                    n_seconds = calendar.monthrange(2025, actual_m)[1] * 86400
                    # Include both boundaries of the month to catch timezone/range errors.
                    offsets = [0, *sorted(rng.sample(range(1, n_seconds - 1), n - 2)), n_seconds - 1]
                    bad_indices = set(rng.sample(range(n), n_bad))
                    for i, offset in enumerate(offsets):
                        time = (start + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        observation_id = (len(tasks) * 2 + month_offset) * 1000 + i
                        writer.writerow((task_id, observation_id, station, time, int(i in bad_indices)))
    return tasks


def _import_observations(csv_path: Path, db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as db, csv_path.open(encoding="utf-8", newline="") as stream:
        db.execute("""CREATE TABLE observations (
            task_id TEXT NOT NULL, observation_id INTEGER PRIMARY KEY, station TEXT NOT NULL,
            observed_utc TEXT NOT NULL, bad_flag INTEGER NOT NULL CHECK(bad_flag IN (0, 1)))""")
        reader = csv.DictReader(stream)
        db.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                       (tuple(row[key] for key in RAW_FIELDS) for row in reader))
        db.execute("CREATE INDEX station_time ON observations(task_id,station,observed_utc)")
        db.commit()


def _reference_from_raw(csv_path: Path, tasks: list[dict]) -> int:
    """Independent UTC parsing/reference totals; never invokes the SQLite query tool."""
    metadata = {task["id"]: task for task in tasks}
    counts = defaultdict(lambda: [0, 0])
    digests = {tid: hashlib.sha256() for tid in metadata}
    seen = set()
    with csv_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            task = metadata[row["task_id"]]
            observed = datetime.strptime(row["observed_utc"], "%Y-%m-%dT%H:%M:%SZ")
            month = observed.strftime("%Y-%m")
            observation_id = int(row["observation_id"])
            if observation_id in seen or row["bad_flag"] not in ("0", "1"):
                raise ValueError("原始观测编号重复或标记无效")
            if row["station"] != task["station"] or month not in (task["month"], previous_month(task["month"])):
                raise ValueError("原始观测不属于该站点和任务快照")
            seen.add(observation_id)
            counts[(task["id"], month)][0] += 1
            counts[(task["id"], month)][1] += int(row["bad_flag"])
            digests[task["id"]].update((",".join(row[key] for key in RAW_FIELDS) + "\n").encode("utf-8"))
    for task in tasks:
        target = counts[task["id"], task["month"]]
        prior = counts[task["id"], previous_month(task["month"])]
        if not (60 <= target[0] <= 140 and 60 <= prior[0] <= 140):
            raise ValueError("月份观测数超出设计范围")
        if (target == prior) != task["collision"]:
            raise ValueError("任务分层与原始数据不一致")
        task.update(total=target[0], bad=target[1], snapshot_sha256=digests[task["id"]].hexdigest())
    return len(seen)


def _load_existing(data: Path, seed: int) -> list[dict]:
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    if manifest["seed"] != seed or manifest["version"] != DATA_VERSION:
        raise ValueError("此目录绑定了不同的数据版本或随机种子，请选新的运行目录")
    for name in ("observations.csv", "observations.sqlite3", "tasks.json"):
        if _file_hash(data / name) != manifest["sha256"][name]:
            raise ValueError(f"数据校验失败: {name}")
    return json.loads((data / "tasks.json").read_text(encoding="utf-8"))


def prepare(root: Path, seed: int) -> list[dict]:
    """Publish a complete immutable dataset; repeated calls verify its manifest."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    if data.exists():
        return _load_existing(data, seed)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        staged = Path(tmp) / "data"
        staged.mkdir()
        raw = staged / "observations.csv"
        tasks = _generate_csv(raw, seed)
        size = _reference_from_raw(raw, tasks)
        _import_observations(raw, staged / "observations.sqlite3")
        _write_json(staged / "tasks.json", tasks)
        checksums = {f.name: _file_hash(f) for f in sorted(staged.iterdir())}
        _write_json(staged / "manifest.json",
                    dict(version=DATA_VERSION, seed=seed, tasks=len(tasks),
                         observations=size, sha256=checksums))
        try:
            staged.rename(data)
        except OSError:
            if not data.exists():
                raise
            return _load_existing(data, seed)
    return tasks


def query(root: Path, task: dict, station: str, month: str, receipt_id: str) -> dict:
    """Read only this task's snapshot. Record actual SQL parameters and row IDs."""
    path = (Path(root) / "data" / "observations.sqlite3").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """SELECT observation_id, bad_flag FROM observations
               WHERE task_id=? AND station=? AND substr(observed_utc,1,7)=?
               ORDER BY observation_id""",
            (task["id"], station, month),
        ).fetchall()
    ids = [obs_id for obs_id, flag in rows]
    bad_ids = [obs_id for obs_id, flag in rows if flag == 1]
    return dict(id=receipt_id, station=station, month=month, total=len(ids),
                bad=len(bad_ids), observation_ids=ids, bad_observation_ids=bad_ids)


def validate(task: dict, report: dict | None, receipts: list[dict]) -> dict:
    """Receipts must be the trusted query log of this branch, never model-supplied data."""
    if report is None:
        return dict(numeric=False, evidence=False, status="stopped")
    numeric = all(type(report.get(k)) is int and report[k] == task[k] for k in ("total", "bad"))
    evidence = False
    matches = [r for r in receipts if r["id"] == report.get("receipt_id")]
    if numeric and len(matches) == 1:
        rec = matches[0]
        ids, bad_ids = rec["observation_ids"], rec["bad_observation_ids"]
        evidence = (all(rec[k] == task[k] == report.get(k) for k in ("station", "month"))
                    and len(set(ids)) == len(ids) == rec["total"] == report["total"]
                    and len(set(bad_ids)) == len(bad_ids) == rec["bad"] == report["bad"]
                    and set(bad_ids).issubset(ids))
    return dict(numeric=numeric, evidence=evidence, status="success" if evidence else "wrong")


def _advance(root: Path, task: dict, state: dict, fault: str, run_id: str,
             step: str, replace: bool) -> None:
    if step == "S1":
        # Normal extraction and the registered S1 intervention both parse the request.
        state["intent"] = parse_request(state["request"])
    elif step == "S2":
        outgoing = state["intent"].copy()
        if fault == "F2" and not replace:
            outgoing["month"] = previous_month(outgoing["month"])
        state["args"] = outgoing
    elif step == "S3":
        actual = state["intent"].copy() if replace else state["args"].copy()
        if fault == "F3" and not replace:
            actual["month"] = previous_month(actual["month"])
        # Opaque, reproducible per branch; parent receipts remain valid at an S4 fork.
        receipt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"qc-receipt/{run_id}").hex
        state["receipt"] = query(root, task, actual["station"], actual["month"], receipt_id)
    else:
        rec = state["receipt"]
        mismatch = any(rec[k] != state["intent"][k] for k in ("month", "station"))
        if replace and mismatch:
            state["report"] = None
        else:
            state["report"] = dict(**state["intent"], total=rec["total"],
                                   bad=rec["bad"], receipt_id=rec["id"])


def simulate(root: Path, task: dict, fault: str, run_id: str) -> tuple[dict, dict]:
    if fault not in FAULTS:
        raise ValueError(f"未知故障条件: {fault}")
    state = {"request": task["request"]}
    checkpoints = {}
    for step in STEPS:
        checkpoints[step] = copy.deepcopy(state)
        _advance(root, task, state, fault, run_id, step, replace=False)
    return state, checkpoints


def replay(root: Path, task: dict, fault: str, run_id: str, checkpoints: dict,
           step: str, arm: str) -> dict:
    """Restore only the prefix and rerun the entire suffix, including live SQLite reads."""
    if fault not in FAULTS or step not in STEPS or arm not in ("original", "replace"):
        raise ValueError("非法的故障、干预位置或对照组")
    state = copy.deepcopy(checkpoints[step])
    branch_id = f"{run_id}/replay/{fault}/{step}/{arm}"
    for current in STEPS[STEPS.index(step):]:
        _advance(root, task, state, fault, branch_id, current, replace=(current == step and arm == "replace"))
    return state

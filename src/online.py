"""Live, resumable model experiments using a Chat Completions compatible endpoint."""

from __future__ import annotations

import copy
import csv
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .core import previous_month, query, validate
from .deepseek_auth import load_key


JUDGE_PROMPT = """你在独立会话中审阅一次按站点和月份统计的记录。
OK=有充分证据表明整条查询链无故障；S2=任务意图正确，但发往工具的月份错；
S3=发往工具的月份正确，但数据库执行月份错；UNKNOWN=可见字段不足以确定以上哪一种。
db_month 是实际执行月份，out_month 是发往工具的月份，intent_month 是提取的任务月份。
只输出一个 JSON 对象，有 label 和 evidence 两个字段。evidence 是一个字典，字段名和原值
必须来自你实际看到的记录，引用足以支持判断的字段。看不到的字段不得猜测。"""

WORKER_PROMPT = """你负责完成用户的海洋观测统计任务。每次通过一个工具采取一项行动。
先发起查询，再依据真实查询回执提交报告。核对回执与请求；有需要时可再查一次，
全程最多查询两次。信息不足时可以停止。提交时引用支持该报告的本轮回执 id。"""


def _tool(name, description, properties):
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False,
        },
    }}


QUERY_FIELDS = {"station": {"type": "string"}, "month": {"type": "string", "description": "YYYY-MM"}}
QUERY = _tool("query_observations", "查询站点在指定年月的观测和坏旗计数", QUERY_FIELDS)
SUBMIT = _tool("submit_report", "用本轮回执提交统计", {
    **QUERY_FIELDS, "total": {"type": "integer"}, "bad": {"type": "integer"},
    "receipt_id": {"type": "string"},
})
STOP = _tool("stop", "停止并说明原因", {"reason": {"type": "string"}})


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(_dump(value) + "\n", encoding="utf-8")
    temp.replace(path)


def _records(path):
    """Recover a torn last append after interruption; complete malformed rows are errors."""
    if not path.exists():
        return []
    items = []
    with path.open("rb+") as stream:
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                stream.truncate(offset)
                break
            items.append(json.loads(line))
    return items


def _append(path, record):
    with path.open("ab") as stream:
        stream.write((_dump(record) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


class BudgetExceeded(RuntimeError):
    pass


class ModelError(RuntimeError):
    """A recorded failed request; never turn this into a successful model outcome."""


class EndpointError(RuntimeError):
    """Provider configuration errors stop the batch before more calls are spent."""


def _env_float(name, default=None):
    value = os.getenv(name)
    number = float(value) if value else default
    if number is not None and (not math.isfinite(number) or number < 0):
        raise ValueError(f"{name} 必须是非负有限值")
    return number


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """max_calls caps HTTP attempts for this invocation, including transport retries."""

    def __init__(self, root: Path, max_calls: int):
        configured_base = os.getenv("AGENT_BASE_URL", "").strip().rstrip("/")
        base = configured_base or "https://api.deepseek.com"
        parsed = urllib.parse.urlsplit(base)
        host = parsed.hostname or ""
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.lower() == "localhost"
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.scheme not in {"http", "https"}
                or (not local and parsed.scheme != "https")):
            raise ValueError("远程端点须使用 HTTPS，地址不能包含凭据、查询参数或片段。")
        self.deepseek = (parsed.scheme == "https" and host.lower() == "api.deepseek.com"
                         and parsed.port in (None, 443) and parsed.path in ("", "/v1"))
        model = os.getenv("AGENT_MODEL", "").strip() or ("deepseek-flash" if self.deepseek else "")
        if not model:
            raise ValueError("自定义端点需要设置 AGENT_MODEL。")
        key = os.getenv("AGENT_API_KEY", "").strip() if configured_base else ""
        if self.deepseek:
            key = os.getenv("DEEPSEEK_API_KEY", "").strip() or key or load_key()
            if not key:
                raise ValueError("请运行 python setup_deepseek.py 或设置 DEEPSEEK_API_KEY。")
        if not local and not key:
            raise ValueError("远程模型需要在环境变量 AGENT_API_KEY 中提供密钥。")
        if max_calls < 1:
            raise ValueError("max_calls 至少为 1。")
        self.dir = root / "online"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.url = base + "/chat/completions"
        self.key = key
        self.model = model
        self.temperature = _env_float("AGENT_TEMPERATURE")
        self.price_in = _env_float("AGENT_INPUT_USD_PER_MTOK")
        self.price_out = _env_float("AGENT_OUTPUT_USD_PER_MTOK")
        self.timeout = _env_float("AGENT_TIMEOUT", 60)
        if self.timeout == 0:
            raise ValueError("AGENT_TIMEOUT 必须大于零。")
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.remaining = max_calls
        config = {"model": model, "base_url": base, "temperature": self.temperature,
                  "input_usd_per_mtok": self.price_in, "output_usd_per_mtok": self.price_out,
                  "timeout_seconds": self.timeout,
                  "protocol_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if self.deepseek:
            config["thinking"] = "disabled"
        manifest = self.dir / "settings.json"
        if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != config:
            raise ValueError("本次模型设置或协议与现有在线结果不同，请使用独立输出目录。")
        _write_json(manifest, config)
        self.events = _records(self.dir / "api_events.jsonl")
        self.starts = {}
        self.finished = {}
        for entry in self.events:
            if entry["kind"] == "start":
                self.starts[entry["id"]] = entry
            elif entry.get("configuration_error"):
                self.starts.pop(entry["id"], None)
            elif entry["kind"] == "success" or entry.get("terminal"):
                self.finished[entry["id"]] = entry
        self.usage()

    def usage(self):
        starts = sum(e["kind"] == "start" for e in self.events)
        successes = [e for e in self.events if e["kind"] == "success"]
        tracked = [e for e in successes if all(isinstance(e["usage"].get(k), int)
                   for k in ("prompt_tokens", "completion_tokens"))]
        tokens_in = sum(e["usage"]["prompt_tokens"] for e in tracked)
        tokens_out = sum(e["usage"]["completion_tokens"] for e in tracked)
        usd = None
        if self.price_in is not None and self.price_out is not None:
            usd = (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000
        _write_json(self.dir / "usage.json", {
            "http_attempts": starts, "successful_responses": len(successes),
            "errors": sum(e["kind"] == "error" for e in self.events),
            "responses_with_token_counts": len(tracked), "known_prompt_tokens": tokens_in,
            "known_completion_tokens": tokens_out, "estimated_usd_for_known_usage_only": usd,
            "recorded_latency_seconds": round(sum(e.get("seconds", 0) for e in self.events), 3),
        })

    def _event(self, event):
        _append(self.dir / "api_events.jsonl", event)
        self.events.append(event)
        if event["kind"] == "start":
            self.starts[event["id"]] = event
        elif event.get("configuration_error"):
            self.starts.pop(event["id"], None)
        elif event["kind"] == "success" or event.get("terminal"):
            self.finished[event["id"]] = event

    def chat(self, call_id, messages, tools=None):
        body = {"model": self.model, "messages": messages}
        if self.deepseek:
            body["thinking"] = {"type": "disabled"}
            if not tools:
                body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
        if self.temperature is not None:
            body["temperature"] = self.temperature
        payload = _dump(body).encode()
        digest = hashlib.sha256(payload).hexdigest()
        previous = self.starts.get(call_id)
        if previous and previous["digest"] != digest:
            raise RuntimeError(f"请求 {call_id} 与之前运行的输入不同，请换独立运行目录。")
        done = self.finished.get(call_id)
        if done is not None:
            if done["kind"] == "success":
                return done["message"]
            raise ModelError(done["error"])
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        first = 1 if previous is None else previous["attempt"] + 1
        for attempt in range(first, 3):
            if self.remaining <= 0:
                raise BudgetExceeded("本次 HTTP 调用预算已用尽。使用同一输出目录和设置可续跑。")
            self.remaining -= 1
            self._event({"id": call_id, "attempt": attempt, "kind": "start",
                         "digest": digest, "request": body, "unix_time": time.time()})
            started = time.monotonic()
            retry = False
            try:
                req = urllib.request.Request(self.url, data=payload, headers=headers)
                with self.opener.open(req, timeout=self.timeout) as response:
                    answer = json.load(response)
                source = answer["choices"][0]["message"]
                if not isinstance(source, dict) or source.get("role") != "assistant":
                    raise ValueError("invalid assistant response")
                message = {k: source[k] for k in ("role", "content", "tool_calls") if k in source}
                self._event({"id": call_id, "attempt": attempt, "kind": "success",
                             "seconds": round(time.monotonic() - started, 3),
                             "message": message, "usage": answer.get("usage") or {},
                             "served_model": answer.get("model")})
                self.usage()
                return message
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
                if exc.code in (400, 401, 402, 403, 404, 405):
                    self._event({"id": call_id, "attempt": attempt, "kind": "error",
                                 "error": error, "configuration_error": True,
                                 "seconds": round(time.monotonic() - started, 3)})
                    self.usage()
                    raise EndpointError(error + "：请检查端点、模型名、授权、余额及工具调用支持。") from None
                retry = exc.code in (408, 429, 500, 502, 503, 504)
            except (TimeoutError, urllib.error.URLError, http.client.HTTPException, ConnectionError) as exc:
                error = f"transport:{type(exc).__name__}"
                retry = True
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                error = f"response_format:{type(exc).__name__}"
            terminal = not retry or attempt == 2
            self._event({"id": call_id, "attempt": attempt, "kind": "error", "error": error,
                         "seconds": round(time.monotonic() - started, 3), "terminal": terminal})
            self.usage()
            if terminal:
                raise ModelError(error)
            time.sleep(1)
        raise ModelError("请求记录显示两次调用均已启动，最终响应无法恢复。")


def run_judges(root: Path, jobs: list[dict], client):
    path = root / "online" / "judgments.jsonl"
    done = {row["id"] for row in _records(path)}
    for job in jobs:
        key = job["id"]
        if key in done:
            continue
        messages = [{"role": "system", "content": JUDGE_PROMPT},
                    {"role": "user", "content": _dump(job["payload"])}]
        record = {"id": key, "label": None, "evidence": {}}
        try:
            response = client.chat("judge/" + key, messages)
            record["raw"] = response.get("content")
            value = json.loads(response["content"])
            if (not isinstance(value, dict) or value.get("label") not in ("S2", "S3", "OK", "UNKNOWN")
                    or not isinstance(value.get("evidence"), dict)):
                raise ValueError("label/evidence schema")
            record.update(label=value["label"], evidence=value["evidence"], status="ok")
        except (KeyError, TypeError, ValueError) as exc:
            record.update(status="format_error", error=type(exc).__name__)
        except ModelError as exc:
            record.update(status="api_error", error=str(exc))
        _append(path, record)
        done.add(key)


def _one_tool(message, permitted):
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("expected_one_tool")
    call = calls[0]
    if (not isinstance(call, dict) or call.get("type") != "function"
            or not isinstance(call.get("id"), str) or not call["id"]):
        raise ValueError("invalid_tool_call")
    spec = call.get("function")
    if not isinstance(spec, dict) or spec.get("name") not in permitted:
        raise ValueError("unexpected_tool")
    args = json.loads(spec["arguments"])
    if not isinstance(args, dict):
        raise ValueError("invalid_arguments")
    return call["id"], spec["name"], args


def _is_query(args):
    return (set(args) == {"station", "month"} and isinstance(args["station"], str) and bool(args["station"].strip())
            and isinstance(args["month"], str)
            and re.fullmatch(r"[1-9][0-9]{3}-(0[1-9]|1[0-2])", args["month"]) is not None)


def _prefix(task, client, key):
    messages = [{"role": "system", "content": WORKER_PROMPT},
                {"role": "user", "content": task["request"]}]
    first = client.chat(key, messages, [QUERY, SUBMIT, STOP])
    _, _, args = _one_tool(first, {"query_observations"})
    if not _is_query(args) or args != {"station": task["station"], "month": task["month"]}:
        raise ValueError("first_query_mismatched")
    return {"messages": messages, "first": first}


def _worker_run(root, task, prefix, fault, step, arm, key, client):
    # Restore the stored first proposal. S2 and S3 are adjacent, without a model call between them.
    messages = copy.deepcopy(prefix["messages"])
    first = copy.deepcopy(prefix["first"])
    tool_id, _, requested = _one_tool(first, {"query_observations"})
    outgoing = requested.copy()
    if fault == "F2":
        outgoing["month"] = previous_month(requested["month"])
    if step == "S2" and arm == "replace":
        outgoing = requested.copy()
    actual = outgoing.copy()
    if fault == "F3":
        actual["month"] = previous_month(outgoing["month"])
    if step == "S3" and arm == "replace":
        actual = requested.copy()
    first["tool_calls"][0]["function"]["arguments"] = _dump(outgoing)
    messages.append(first)
    receipts = []
    # IDs are opaque to the worker and stable across retries of the same branch.
    def execute(args, call_id):
        receipt_id = uuid.uuid5(uuid.NAMESPACE_URL, key + f"/query/{len(receipts)}").hex
        receipt = query(root, task, args["station"], args["month"], receipt_id)
        receipts.append(receipt)
        visible = {k: receipt[k] for k in ("id", "station", "month", "total", "bad")}
        messages.append({"role": "tool", "tool_call_id": call_id, "content": _dump(visible)})

    record = {"outgoing": outgoing, "actual": actual, "receipts": receipts,
              "report": None, "stop_reason": None}
    execute(actual, tool_id)
    try:
        for turn in (1, 2):
            assistant = client.chat(f"{key}/turn{turn}", messages,
                                    [QUERY, SUBMIT, STOP] if turn == 1 else [SUBMIT, STOP])
            call_id, name, args = _one_tool(assistant, {"query_observations", "submit_report", "stop"})
            messages.append(assistant)
            if name == "query_observations":
                if turn == 2 or not _is_query(args):
                    raise ValueError("invalid_requery")
                execute(args, call_id)
                continue
            if name == "submit_report":
                if (set(args) != {"station", "month", "total", "bad", "receipt_id"}
                        or not _is_query({k: args.get(k) for k in QUERY_FIELDS})
                        or type(args.get("total")) is not int or type(args.get("bad")) is not int
                        or not isinstance(args.get("receipt_id"), str)):
                    raise ValueError("invalid_report")
                record["report"] = args
            else:
                if set(args) != {"reason"} or not isinstance(args.get("reason"), str):
                    raise ValueError("invalid_stop_reason")
                record["stop_reason"] = args["reason"]
            record.update(validate(task, record["report"], receipts))
            record["outcome"] = ("stop" if name == "stop" else
                                 "success" if record["evidence"] else "incorrect")
            break
    except ModelError as exc:
        record.update(outcome="api_error", error=str(exc))
    except (ValueError, TypeError, KeyError) as exc:
        record.update(outcome="format_error", error=str(exc))
    record["queries"] = len(receipts)
    return record


def _csv(path, fields, rows):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def check_tools(root, tasks, client):
    """One clean task to exercise a real tool call and its continuation."""
    task = next(t for t in tasks if t["id"] == "A-2025-09")
    # Use new request IDs to check connectivity on every invocation.
    key = "connectivity/" + uuid.uuid4().hex
    prefix = _prefix(task, client, key + "/prefix")
    result = _worker_run(root, task, prefix, "F0", None, None, key, client)
    record = {"id": key, "task": task["id"], "model": client.model,
              "outcome": result["outcome"], "queries": result["queries"],
              "report": result["report"], "evidence": result.get("evidence", False)}
    _append(client.dir / "connectivity.jsonl", record)
    if not record["evidence"]:
        raise RuntimeError(f"模型调用已完成，本次工具任务未通过证据验收：{record['outcome']}。")
    return record


def _worker_tables(folder, selected, records):
    initial, replay = [], []
    for task in selected:
        tid = task["id"]
        clean = records.get(f"{tid}/baseline/F0", {})
        for fault in ("F0", "F2", "F3"):
            base = records.get(f"{tid}/baseline/{fault}", {})
            status = base.get("outcome", "not_run")
            eligible = clean.get("outcome") == "success" and status in ("incorrect", "stop")
            attempted = [records.get(f"{tid}/prefix/{i}", {}) for i in (1, 2)]
            prefix_state = ("qualified" if any(p.get("qualified") for p in attempted)
                            else "exhausted" if all(attempted) else "incomplete")
            initial.append({"task": tid, "fault": fault, "prefix_status": prefix_state,
                            "outcome": status, "queries": base.get("queries"),
                            "eligible": eligible and fault != "F0"})
            if fault == "F0":
                continue
            for step in ("S2", "S3"):
                row = {"task": tid, "fault": fault, "step": step, "eligible": eligible}
                rates = []
                for arm in ("original", "replace"):
                    runs = [records[f"{tid}/{fault}/{step}/{arm}/{rep}"] for rep in range(3)
                            if f"{tid}/{fault}/{step}/{arm}/{rep}" in records]
                    valid = [r for r in runs if r["outcome"] in ("success", "incorrect", "stop")]
                    success = sum(r["outcome"] == "success" for r in valid)
                    row[arm + "_completed"] = len(runs)
                    row[arm + "_valid"] = len(valid)
                    row[arm + "_success"] = success
                    rates.append(success / len(valid) if valid else None)
                row["gain_valid_pp"] = round(100*(rates[1]-rates[0]), 3) if None not in rates else None
                replay.append(row)
    if initial:
        _csv(folder / "worker_summary.csv", list(initial[0]), initial)
    if replay:
        _csv(folder / "worker_replays.csv", list(replay[0]), replay)


def run_workers(root: Path, tasks: list[dict], client):
    selected = []
    for station, month in (("A", "2025-04"), ("B", "2025-10"), ("C", "2025-03"), ("A", "2025-09")):
        selected.append(next(t for t in tasks if t["split"] == "test" and t["station"] == station
                             and t["month"] == month))
    folder = root / "online"
    path = folder / "workers.jsonl"
    records = {r["id"]: r for r in _records(path)}

    def save(key, data):
        row = {"id": key, **data}
        _append(path, row)
        records[key] = row

    try:
        for task in selected:
            tid = task["id"]
            prefix = None
            for attempt in (1, 2):
                key = f"{tid}/prefix/{attempt}"
                if key not in records:
                    try:
                        value = _prefix(task, client, key)
                        row = {"qualified": True, "prefix": value}
                    except (ModelError, ValueError, KeyError, TypeError) as exc:
                        row = {"qualified": False, "reason": str(exc)}
                    save(key, row)
                if records[key]["qualified"]:
                    prefix = records[key]["prefix"]
                    break
            if prefix is None:
                continue
            for fault in ("F0", "F2", "F3"):
                key = f"{tid}/baseline/{fault}"
                if key not in records:
                    save(key, _worker_run(root, task, prefix, fault, None, None, key, client))
            if records[f"{tid}/baseline/F0"]["outcome"] != "success":
                continue
            for fault in ("F2", "F3"):
                if records[f"{tid}/baseline/{fault}"]["outcome"] not in ("incorrect", "stop"):
                    continue
                for step in ("S2", "S3"):
                    for rep in range(3):
                        arms = ("original", "replace") if rep % 2 == 0 else ("replace", "original")
                        for arm in arms:
                            key = f"{tid}/{fault}/{step}/{arm}/{rep}"
                            if key not in records:
                                save(key, _worker_run(root, task, prefix, fault, step, arm, key, client))
    finally:
        _worker_tables(folder, selected, records)

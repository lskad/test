"""用内存中的兼容端点回包校验在线管线；这两项测试不调用模型。"""
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import experiment
from src import core, online


def assistant_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "local-test", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def simulated_endpoint(request, timeout):
    body = json.loads(request.data)
    messages = body["messages"]
    if "tools" not in body:
        payload = json.loads(messages[-1]["content"])
        if set(payload) != {"request", "report"}:
            raise AssertionError("诊断请求泄漏了元数据")
        message = {"role": "assistant", "content": '{"label":"UNKNOWN","evidence":{}}'}
    else:
        intent = core.parse_request(messages[1]["content"])
        if messages[-1]["role"] == "user":
            message = assistant_call("query_observations", intent)
        else:
            receipt = json.loads(messages[-1]["content"])
            message = assistant_call("submit_report", {**intent, "total": receipt["total"],
                                     "bad": receipt["bad"], "receipt_id": receipt["id"]})
    return io.BytesIO(json.dumps({"choices": [{"message": message}], "model": "test-only",
                                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())


class OnlineProtocolTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        environment = patch.dict(os.environ, {
            "AGENT_BASE_URL": "http://127.0.0.1:8000/v1", "AGENT_MODEL": "test-only",
            "AGENT_API_KEY": "", "AGENT_TEMPERATURE": "", "AGENT_TIMEOUT": "",
            "AGENT_INPUT_USD_PER_MTOK": "", "AGENT_OUTPUT_USD_PER_MTOK": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_blind_budget_and_resume(self):
        jobs = [{"id": str(i), "payload": {"request": "一条请求", "report": {}},
                 "private_key": "测试用隐藏真值"} for i in range(2)]
        client = online.Client(self.root, 1)
        with patch.object(client.opener, "open", side_effect=simulated_endpoint):
            with self.assertRaises(online.BudgetExceeded):
                online.run_judges(self.root, jobs, client)
        again = online.Client(self.root, 1)
        with patch.object(again.opener, "open", side_effect=simulated_endpoint) as endpoint:
            online.run_judges(self.root, jobs, again)
            self.assertEqual(endpoint.call_count, 1)
        answers = experiment.read_lines(self.root / "online" / "judgments.jsonl")
        self.assertEqual([a["label"] for a in answers], ["UNKNOWN", "UNKNOWN"])

    def test_deepseek_protocol_is_scoped_to_official_endpoint(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "", "AGENT_MODEL": "",
                                     "DEEPSEEK_API_KEY": "dummy-test-only"}):
            client = online.Client(self.root, 1)
            self.assertEqual(client.model, "deepseek-flash")
            self.assertEqual(client.url, "https://api.deepseek.com/chat/completions")
            def inspect(request, timeout):
                body = json.loads(request.data)
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertEqual(body["response_format"], {"type": "json_object"})
                return simulated_endpoint(request, timeout)
            with patch.object(client.opener, "open", side_effect=inspect):
                online.run_judges(self.root, [{"id": "a", "payload": {"request": "test", "report": {}}}], client)
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://example.com/v1",
                                     "DEEPSEEK_API_KEY": "dummy-test-only"}):
            with patch.object(online, "load_key", side_effect=AssertionError):
                with self.assertRaisesRegex(ValueError, "AGENT_API_KEY"):
                    online.Client(self.root, 1)

    def test_all_worker_pairs_and_checkpoint_resume(self):
        tasks = core.prepare(self.root, 42)
        client = online.Client(self.root, 2)
        with patch.object(client.opener, "open", side_effect=simulated_endpoint):
            with self.assertRaises(online.BudgetExceeded):
                online.run_workers(self.root, tasks, client)
        again = online.Client(self.root, 150)
        with patch.object(again.opener, "open", side_effect=simulated_endpoint):
            online.run_workers(self.root, tasks, again)
        rows = experiment.read_lines(self.root / "online" / "workers.jsonl")
        ids = {r["id"] for r in rows}
        self.assertEqual(len(rows), len(ids))
        repeats = [r for r in rows if "/original/" in r["id"] or "/replace/" in r["id"]]
        self.assertEqual(len(repeats), 96)
        for row in repeats:
            _, fault, step, arm, _ = row["id"].split("/")
            success = arm == "replace" and (fault == "F2" or step == "S3")
            self.assertEqual(row["outcome"] == "success", success)

    def test_worker_can_recover_without_intervention(self):
        tasks = core.prepare(self.root, 19)
        client = online.Client(self.root, 60)

        def recover_if_needed(request, timeout):
            body = json.loads(request.data)
            messages = body["messages"]
            if messages[-1]["role"] == "tool":
                intent = core.parse_request(messages[1]["content"])
                receipt = json.loads(messages[-1]["content"])
                if receipt["month"] != intent["month"]:
                    query = assistant_call("query_observations", intent)
                    return io.BytesIO(json.dumps({"choices": [{"message": query}]}).encode())
            return simulated_endpoint(request, timeout)

        with patch.object(client.opener, "open", side_effect=recover_if_needed):
            online.run_workers(self.root, tasks, client)
        rows = experiment.read_lines(self.root / "online" / "workers.jsonl")
        corrected = [r for r in rows if "/baseline/F" in r["id"] and not r["id"].endswith("F0")]
        self.assertEqual(len(corrected), 8)
        self.assertTrue(all(r["queries"] == 2 and r["outcome"] == "success" for r in corrected))
        self.assertFalse(any("/replace/" in r["id"] for r in rows))


if __name__ == "__main__":
    unittest.main()

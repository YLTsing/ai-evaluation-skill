from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
EVALUATION = SCRIPTS / "evaluation_runner.py"
JUDGE = SCRIPTS / "judge_runner.py"

sys.path.insert(0, str(SCRIPTS))
from runner_common import BENCHMARK_FIELDS, HUMAN_REVIEW_FIELDS, RESULT_FIELDS  # noqa: E402
from runner_common import material_media_type  # noqa: E402
import evaluation_runner as evaluation_module  # noqa: E402
import judge_runner as judge_module  # noqa: E402
from runner_common import RunnerError  # noqa: E402


def write_csv(path: Path, fields, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_material_manifest(root: Path, files) -> None:
    entries = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "media_type": material_media_type(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = root / "benchmark_materials" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"schema_version": "1.0", "benchmark_version": "v1", "files": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


class RunnerEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.benchmark = self.root / "03_benchmark_cases.csv"
        rows = []
        for index in range(1, 3):
            rows.append(
                {
                    "用例编号": "CASE_{:03d}".format(index),
                    "用例类型": "正常场景",
                    "优先级": "P2",
                    "对应评测维度": "准确性",
                    "输入": "问题 {}".format(index),
                    "上下文/素材": "参考 {}".format(index),
                    "预期输出描述": "正确回答",
                    "典型失败模式": "答非所问",
                    "风险等级": "低",
                    "一票否决规则": "无",
                    "人工复核触发条件": "低分或不确定",
                    "备注": "",
                }
            )
        write_csv(self.benchmark, BENCHMARK_FIELDS, rows)
        self.mock_product = self.root / "mock_product.py"
        self.mock_product.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({\n"
            "  'output': '真实输出：' + payload['input'],\n"
            "  'usage': {'model_latency_ms': 12, 'input_tokens': 10, 'output_tokens': 5, 'cost': 0.01, 'currency': 'CNY', 'cost_source': 'runtime-reported'},\n"
            "  'trace_id': payload['case_id'] + '-trace'\n"
            "}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        self.config = self.root / "runner_config.json"
        self.config.write_text(
            json.dumps(
                {
                    "adapter": "command",
                    "command": [sys.executable, str(self.mock_product)],
                    "timeout_seconds": 5,
                    "max_retries": 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.runs = self.root / "runs"
        self.run_id = "run-test"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, *args, expect=0):
        completed = subprocess.run(
            [sys.executable, *map(str, args)], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return completed

    def init_and_run(self) -> Path:
        self.execute(
            EVALUATION,
            "init",
            "--benchmark",
            self.benchmark,
            "--run-root",
            self.runs,
            "--product-slug",
            "demo-product",
            "--product-version",
            "v1",
            "--adapter",
            "command",
            "--run-id",
            self.run_id,
        )
        run_dir = self.runs / self.run_id
        prepared = read_csv(run_dir / "results.csv")
        expected_count = len(read_csv(self.benchmark))
        self.assertEqual([row["候选执行状态"] for row in prepared], ["待执行"] * expected_count)
        self.execute(EVALUATION, "run", "--run-dir", run_dir, "--config", self.config)
        return run_dir

    def test_evaluation_and_judge_flow(self):
        run_dir = self.init_and_run()
        rows = read_csv(run_dir / "results.csv")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["候选执行状态"] == "成功" for row in rows))
        self.assertEqual(rows[0]["输入Token"], "10")
        self.assertEqual(rows[0]["费用来源"], "runtime-reported")

        rubric = self.root / "rubric.md"
        rubric.write_text("# Rubric\n准确性：1-3 分。\n", encoding="utf-8")
        plan = self.root / "judge.md"
        plan.write_text("# Judge\n只基于证据评分。\n", encoding="utf-8")
        prepared = self.execute(
            JUDGE,
            "prepare",
            "--run-dir",
            run_dir,
            "--rubric",
            rubric,
            "--review-plan",
            plan,
        )
        payload = json.loads(prepared.stdout)
        self.assertEqual(len(payload["cases"]), 2)
        self.assertEqual(payload["cases"][0]["failure_modes"], "答非所问")
        judge_results = {
            "results": [
                {
                    "case_id": case["case_id"],
                    "run_sequence": case["run_sequence"],
                    "judge_model": "test-agent",
                    "dimension_scores": {
                        "准确性": {"score": 3, "reason": "符合预期", "evidence": "回答使用了输入"}
                    },
                    "veto_candidate": {"candidate": False, "rules": [], "reason": ""},
                    "human_review": {"required": False, "reason": ""},
                    "uncertainty": "",
                    "preliminary_judgment": "初步通过",
                }
                for case in payload["cases"]
            ]
        }
        judge_file = self.root / "judge_results.json"
        judge_file.write_text(json.dumps(judge_results, ensure_ascii=False), encoding="utf-8")
        self.execute(JUDGE, "apply", "--run-dir", run_dir, "--input", judge_file)
        judged = read_csv(run_dir / "results.csv")
        self.assertTrue(all(row["Judge执行状态"] == "成功" for row in judged))
        reviews = read_csv(run_dir / "human_review.csv")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(list(reviews[0].keys()), HUMAN_REVIEW_FIELDS)
        status_before = self.execute(JUDGE, "status", "--run-dir", run_dir)
        self.assertEqual(json.loads(status_before.stdout)["formal_report_gate"], "fail")
        for review in reviews:
            review.update(
                {
                    "人工评测分数": "3",
                    "人工评测理由": "人工确认符合要求",
                    "是否确认一票否决": "否",
                    "复核人": "tester",
                    "复核时间": "2026-07-14T12:00:00+08:00",
                    "复核状态": "已完成",
                    "人工复核结果": "通过",
                    "最终结论": "通过",
                }
            )
        write_csv(run_dir / "human_review.csv", HUMAN_REVIEW_FIELDS, reviews)
        status_after = self.execute(JUDGE, "status", "--run-dir", run_dir)
        self.assertEqual(json.loads(status_after.stdout)["formal_report_gate"], "pass")

    def test_invalid_judge_does_not_modify_results(self):
        run_dir = self.init_and_run()
        before = (run_dir / "results.csv").read_bytes()
        invalid = self.root / "invalid.json"
        invalid.write_text(
            json.dumps(
                {
                    "case_id": "CASE_001",
                    "run_sequence": 1,
                    "dimension_scores": {"准确性": {"score": 4, "reason": "x", "evidence": "y"}},
                    "veto_candidate": {"candidate": False, "rules": [], "reason": ""},
                    "human_review": {"required": False, "reason": ""},
                    "preliminary_judgment": "x",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.execute(JUDGE, "apply", "--run-dir", run_dir, "--input", invalid, expect=2)
        self.assertEqual(before, (run_dir / "results.csv").read_bytes())

    def test_priority_and_risk_labels_do_not_force_every_record(self):
        case = {"优先级": "P0", "风险等级": "高"}
        judged = {
            "dimension_scores": {"准确性": {"score": 3}},
            "veto_candidate": {"candidate": False},
            "uncertainty": "",
            "human_review": {"required": False, "reason": ""},
        }
        self.assertEqual(judge_module.mandatory_reasons(case, judged), [])

    def test_high_risk_case_selects_one_representative_record(self):
        run_dir = self.root / "review-policy"
        run_dir.mkdir()
        case_id = "RISK_001"
        cases = {
            case_id: {
                "用例编号": case_id,
                "用例类型": "风险场景",
                "优先级": "P0",
                "风险等级": "高",
            }
        }
        rows = []
        for sequence in range(1, 4):
            row = {field: "" for field in RESULT_FIELDS}
            row.update(
                {
                    "评测运行ID": "run-policy",
                    "用例编号": case_id,
                    "运行序号": str(sequence),
                    "候选执行状态": "成功",
                    "模型输出": "output",
                    "Judge执行状态": "成功",
                    "Judge分数JSON": json.dumps(
                        {"准确性": {"score": 3, "reason": "ok", "evidence": "e"}},
                        ensure_ascii=False,
                    ),
                    "一票否决候选": "否",
                    "是否进入人工复核": "否",
                }
            )
            rows.append(row)
        count = judge_module.rebuild_human_review(
            run_dir, "run-policy", rows, cases
        )
        self.assertEqual(count, 1)
        self.assertEqual(sum(row["是否进入人工复核"] == "是" for row in rows), 1)
        self.assertIn(
            "高风险 case 代表复核",
            next(row["人工复核原因"] for row in rows if row["是否进入人工复核"] == "是"),
        )

    def test_multi_run_score_variance_reviews_all_records(self):
        run_dir = self.root / "review-variance"
        run_dir.mkdir()
        case_id = "CASE_VARIANCE"
        cases = {
            case_id: {
                "用例编号": case_id,
                "用例类型": "正常场景",
                "优先级": "P2",
                "风险等级": "低",
            }
        }
        rows = []
        for sequence, score in enumerate((3, 2, 3), 1):
            row = {field: "" for field in RESULT_FIELDS}
            row.update(
                {
                    "评测运行ID": "run-variance",
                    "用例编号": case_id,
                    "运行序号": str(sequence),
                    "候选执行状态": "成功",
                    "模型输出": "output",
                    "Judge执行状态": "成功",
                    "Judge分数JSON": json.dumps(
                        {"准确性": {"score": score, "reason": "r", "evidence": "e"}},
                        ensure_ascii=False,
                    ),
                    "一票否决候选": "否",
                    "是否进入人工复核": "否",
                }
            )
            rows.append(row)
        count = judge_module.rebuild_human_review(
            run_dir, "run-variance", rows, cases
        )
        self.assertEqual(count, 3)
        self.assertTrue(all(row["是否进入人工复核"] == "是" for row in rows))
        self.assertTrue(
            all("多轮评分或否决状态异常" in row["人工复核原因"] for row in rows)
        )

    def test_ordinary_sampling_deduplicates_repeated_runs(self):
        run_dir = self.root / "review-sampling"
        run_dir.mkdir()
        cases = {}
        rows = []
        for case_index in range(1, 11):
            case_id = "NORMAL_{:03d}".format(case_index)
            cases[case_id] = {
                "用例编号": case_id,
                "用例类型": "正常场景",
                "优先级": "P2",
                "风险等级": "低",
            }
            for sequence in range(1, 4):
                row = {field: "" for field in RESULT_FIELDS}
                row.update(
                    {
                        "评测运行ID": "run-sampling",
                        "用例编号": case_id,
                        "运行序号": str(sequence),
                        "候选执行状态": "成功",
                        "模型输出": "output",
                        "Judge执行状态": "成功",
                        "Judge分数JSON": json.dumps(
                            {"准确性": {"score": 3, "reason": "r", "evidence": "e"}},
                            ensure_ascii=False,
                        ),
                        "一票否决候选": "否",
                        "是否进入人工复核": "否",
                    }
                )
                rows.append(row)
        count = judge_module.rebuild_human_review(
            run_dir, "run-sampling", rows, cases
        )
        selected = [row for row in rows if row["是否进入人工复核"] == "是"]
        self.assertEqual(count, 2)
        self.assertEqual(len({row["用例编号"] for row in selected}), 2)

    def test_legacy_schema_is_rejected_without_migration(self):
        legacy = self.root / "legacy.csv"
        fields = BENCHMARK_FIELDS + ["模型输出", "评测运行ID"]
        row = {field: "" for field in fields}
        row.update({"用例编号": "OLD_001", "输入": "旧问题", "模型输出": "旧输出"})
        write_csv(legacy, fields, [row])
        completed = self.execute(
            EVALUATION,
            "init",
            "--benchmark",
            legacy,
            "--run-root",
            self.runs,
            "--product-slug",
            "legacy",
            "--product-version",
            "v1",
            "--adapter",
            "import",
            expect=2,
        )
        self.assertIn("migrate", completed.stderr)

    def test_import_real_results(self):
        run_id = "run-import"
        self.execute(
            EVALUATION,
            "init",
            "--benchmark",
            self.benchmark,
            "--run-root",
            self.runs,
            "--product-slug",
            "import-product",
            "--product-version",
            "v1",
            "--adapter",
            "import",
            "--run-id",
            run_id,
        )
        imported = self.root / "import.json"
        imported.write_text(
            json.dumps(
                [
                    {
                        "case_id": "CASE_001",
                        "run_sequence": 1,
                        "output": "导入的真实输出",
                        "usage": {"input_tokens": 8, "output_tokens": 4},
                        "end_to_end_latency_ms": 30,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.execute(
            EVALUATION,
            "import-results",
            "--run-dir",
            self.runs / run_id,
            "--input",
            imported,
            "--source",
            "product-export-001",
        )
        rows = read_csv(self.runs / run_id / "results.csv")
        self.assertEqual(rows[0]["候选执行状态"], "成功")
        self.assertEqual(rows[0]["模型输出"], "导入的真实输出")
        self.assertEqual(rows[1]["候选执行状态"], "待执行")

    def test_legacy_migration_is_non_destructive(self):
        legacy = self.root / "legacy.csv"
        execution_fields = [
            "模型版本/方案",
            "评测运行ID",
            "运行序号",
            "模型输出",
            "Judge结果JSON",
            "LLM-as-Judge 分数",
            "LLM-as-Judge 评分理由",
            "一票否决候选",
            "是否进入人工复核",
            "人工评测分数",
            "人工评测理由",
            "是否触发一票否决",
            "复核人",
            "复核状态",
            "人工复核结果",
            "最终结论",
        ]
        fields = BENCHMARK_FIELDS + execution_fields
        row = {field: "" for field in fields}
        row.update(
            {
                "用例编号": "OLD_001",
                "用例类型": "回归场景",
                "优先级": "P1",
                "对应评测维度": "准确性",
                "输入": "旧问题",
                "预期输出描述": "旧预期",
                "风险等级": "高",
                "模型版本/方案": "v-old",
                "评测运行ID": "run-old",
                "运行序号": "1",
                "模型输出": "旧输出",
                "LLM-as-Judge 分数": "3",
                "LLM-as-Judge 评分理由": "旧理由",
                "人工评测分数": "3",
                "复核状态": "已完成",
                "人工复核结果": "通过",
                "最终结论": "通过",
            }
        )
        write_csv(legacy, fields, [row])
        before = legacy.read_bytes()
        migrated = self.root / "migrated"
        self.execute(
            EVALUATION,
            "migrate",
            "--source",
            legacy,
            "--output-dir",
            migrated,
            "--product-slug",
            "legacy-product",
        )
        self.assertEqual(before, legacy.read_bytes())
        benchmark_rows = read_csv(migrated / "03_benchmark_cases.csv")
        self.assertEqual(list(benchmark_rows[0].keys()), BENCHMARK_FIELDS)
        run_dirs = list((migrated / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "results.csv").exists())
        self.assertTrue((run_dirs[0] / "human_review.csv").exists())

    def test_http_adapter_and_secret_guard(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"output": "HTTP 输出"}, ensure_ascii=False).encode("utf-8")

        with patch.object(evaluation_module.urllib.request, "urlopen", return_value=FakeResponse()):
            response = evaluation_module.call_http(
                {"url": "https://product.example.test/evaluate", "headers": {}}, {"input": "问题"}
            )
        self.assertEqual(response["output"], "HTTP 输出")
        with self.assertRaises(RunnerError):
            evaluation_module.call_http(
                {
                    "url": "https://product.example.test/evaluate",
                    "headers": {"Authorization": "Bearer plaintext-secret"},
                },
                {"input": "问题"},
            )

    def test_material_refs_expand_and_evaluation_fields_do_not_leak(self):
        materials = self.root / "benchmark_materials" / "MAT_001"
        materials.mkdir(parents=True)
        input_file = materials / "input.md"
        context_file = materials / "context.txt"
        input_file.write_text("完整用户任务", encoding="utf-8")
        context_file.write_text("完整参考材料", encoding="utf-8")
        write_material_manifest(self.root, [input_file, context_file])
        rows = read_csv(self.benchmark)
        rows[0]["输入"] = json.dumps({"$material_ref": "benchmark_materials/MAT_001/input.md"}, ensure_ascii=False)
        rows[0]["上下文/素材"] = json.dumps(
            {"$material_refs": {"document": "benchmark_materials/MAT_001/context.txt"}}, ensure_ascii=False
        )
        write_csv(self.benchmark, BENCHMARK_FIELDS, rows[:1])
        capture_product = self.root / "capture_product.py"
        capture_product.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({'output': json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        self.config.write_text(
            json.dumps({"adapter": "command", "command": [sys.executable, str(capture_product)]}),
            encoding="utf-8",
        )
        run_dir = self.init_and_run()
        result = read_csv(run_dir / "results.csv")[0]
        candidate_payload = json.loads(result["模型输出"])
        self.assertEqual(
            set(candidate_payload), {"run_id", "case_id", "run_sequence", "input", "context"}
        )
        self.assertEqual(candidate_payload["input"], "完整用户任务")
        self.assertEqual(json.loads(candidate_payload["context"]), {"document": "完整参考材料"})
        self.assertNotIn("正确回答", json.dumps(candidate_payload, ensure_ascii=False))
        manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["benchmark_bundle_sha256"])
        self.assertTrue(manifest["material_manifest_sha256"])
        rubric = self.root / "material-rubric.md"
        plan = self.root / "material-judge.md"
        rubric.write_text("# Rubric\n准确性：1-3 分。\n", encoding="utf-8")
        plan.write_text("# Judge\n只基于证据评分。\n", encoding="utf-8")
        judge_payload = json.loads(
            self.execute(
                JUDGE, "prepare", "--run-dir", run_dir, "--rubric", rubric, "--review-plan", plan
            ).stdout
        )
        self.assertEqual(judge_payload["cases"][0]["user_input"], "完整用户任务")
        self.assertEqual(
            json.loads(judge_payload["cases"][0]["context"]), {"document": "完整参考材料"}
        )
        self.assertEqual(judge_payload["cases"][0]["expected_output"], "正确回答")
        self.assertEqual(judge_payload["cases"][0]["failure_modes"], "答非所问")

    def test_material_change_blocks_existing_run(self):
        materials = self.root / "benchmark_materials" / "MAT_001"
        materials.mkdir(parents=True)
        input_file = materials / "input.md"
        input_file.write_text("冻结前内容", encoding="utf-8")
        write_material_manifest(self.root, [input_file])
        rows = read_csv(self.benchmark)
        rows[0]["输入"] = json.dumps({"$material_ref": "benchmark_materials/MAT_001/input.md"}, ensure_ascii=False)
        write_csv(self.benchmark, BENCHMARK_FIELDS, rows[:1])
        self.execute(
            EVALUATION, "init", "--benchmark", self.benchmark, "--run-root", self.runs,
            "--product-slug", "demo-product", "--product-version", "v1", "--adapter", "command",
            "--run-id", self.run_id,
        )
        input_file.write_text("冻结后被修改", encoding="utf-8")
        completed = self.execute(
            EVALUATION, "run", "--run-dir", self.runs / self.run_id, "--config", self.config, expect=2
        )
        self.assertIn("摘要不匹配", completed.stderr)

    def test_material_path_escape_is_rejected(self):
        rows = read_csv(self.benchmark)
        rows[0]["输入"] = json.dumps({"$material_ref": "../outside.txt"}, ensure_ascii=False)
        write_csv(self.benchmark, BENCHMARK_FIELDS, rows[:1])
        completed = self.execute(
            EVALUATION, "init", "--benchmark", self.benchmark, "--run-root", self.runs,
            "--product-slug", "demo-product", "--product-version", "v1", "--adapter", "command",
            expect=2,
        )
        self.assertIn("越出 Benchmark 目录", completed.stderr)

    def test_binary_material_is_passed_as_descriptor(self):
        materials = self.root / "benchmark_materials" / "MM_001"
        materials.mkdir(parents=True)
        image_file = materials / "image.png"
        image_file.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-test-data")
        write_material_manifest(self.root, [image_file])
        rows = read_csv(self.benchmark)
        rows[0]["上下文/素材"] = json.dumps(
            {"$material_refs": {"image": "benchmark_materials/MM_001/image.png"}}, ensure_ascii=False
        )
        write_csv(self.benchmark, BENCHMARK_FIELDS, rows[:1])
        capture_product = self.root / "capture_product.py"
        capture_product.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({'output': payload['context']}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        self.config.write_text(
            json.dumps({"adapter": "command", "command": [sys.executable, str(capture_product)]}),
            encoding="utf-8",
        )
        run_dir = self.init_and_run()
        descriptor = json.loads(read_csv(run_dir / "results.csv")[0]["模型输出"])["image"]
        self.assertEqual(descriptor["type"], "file")
        self.assertEqual(descriptor["media_type"], "image/png")
        self.assertEqual(descriptor["sha256"], hashlib.sha256(image_file.read_bytes()).hexdigest())
        self.assertTrue(Path(descriptor["path"]).is_absolute())


if __name__ == "__main__":
    unittest.main()

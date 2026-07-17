from __future__ import annotations

import tempfile
import unittest
import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

import sys

sys.path.insert(0, str(SCRIPTS))
from validate_outputs import validate  # noqa: E402


class OutputValidatorTest(unittest.TestCase):
    def test_rejects_markdown_summary_that_drops_template_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            product = Path(temp)
            (product / "01_评测方案.md").write_text(
                "# 评测方案\n\n## 评测目标\n\n只保留摘要。\n", encoding="utf-8"
            )
            errors = validate(product, require_default_package=False)
            self.assertTrue(any("01_评测方案.md 缺少或错序章节" in error for error in errors))

    def test_standard_template_package_passes_validation(self):
        mapping = {
            "01_评测方案.md": "01_evaluation_plan_template.md",
            "02_Benchmark用例设计.md": "02_benchmark_design_template.md",
            "03_benchmark_cases.csv": "03_benchmark_cases_template.csv",
            "04_Rubric评分标准.md": "04_rubric_template.md",
            "05_LLM-as-Judge评审方案.md": "05_llm_as_judge_review_plan_template.md",
            "07_bad_case_log.csv": "07_bad_case_log_template.csv",
            "08_持续评测机制.md": "08_continuous_evaluation_template.md",
        }
        with tempfile.TemporaryDirectory() as temp:
            product = Path(temp)
            for output_name, template_name in mapping.items():
                (product / output_name).write_bytes((ROOT / "templates" / template_name).read_bytes())
            self.assertEqual(validate(product, require_default_package=True), [])

    def test_material_refs_require_valid_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            product = Path(temp)
            material = product / "benchmark_materials" / "CASE_001" / "input.txt"
            material.parent.mkdir(parents=True)
            material.write_text("真实长度输入", encoding="utf-8")
            benchmark = product / "03_benchmark_cases.csv"
            fields = [
                "用例编号", "用例类型", "优先级", "对应评测维度", "输入", "上下文/素材",
                "预期输出描述", "典型失败模式", "风险等级", "一票否决规则", "人工复核触发条件", "备注",
            ]
            with benchmark.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "用例编号": "CASE_001", "用例类型": "正常场景", "优先级": "P2",
                    "对应评测维度": "准确性",
                    "输入": json.dumps({"$material_ref": "benchmark_materials/CASE_001/input.txt"}),
                    "上下文/素材": "", "预期输出描述": "正确", "典型失败模式": "错误",
                    "风险等级": "低", "一票否决规则": "无", "人工复核触发条件": "低分", "备注": "",
                })
            errors = validate(product, require_default_package=False)
            self.assertTrue(any("缺少 benchmark_materials/manifest.json" in error for error in errors))
            manifest = product / "benchmark_materials" / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "1.0", "benchmark_version": "v1", "files": [{
                    "path": "benchmark_materials/CASE_001/input.txt", "media_type": "text/plain",
                    "sha256": hashlib.sha256(material.read_bytes()).hexdigest(),
                }]
            }), encoding="utf-8")
            self.assertEqual(validate(product, require_default_package=False), [])


if __name__ == "__main__":
    unittest.main()

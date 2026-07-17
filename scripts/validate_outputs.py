#!/usr/bin/env python3
"""Validate generated evaluation assets against templates and runner schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Sequence

from runner_common import (
    BENCHMARK_FIELDS,
    HUMAN_REVIEW_FIELDS,
    RESULT_FIELDS,
    RunnerError,
    benchmark_bundle_sha256,
    read_benchmark,
    read_csv,
    validate_material_manifest,
)


ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_MAP = {
    "01_评测方案.md": "templates/01_evaluation_plan_template.md",
    "02_Benchmark用例设计.md": "templates/02_benchmark_design_template.md",
    "04_Rubric评分标准.md": "templates/04_rubric_template.md",
    "05_LLM-as-Judge评审方案.md": "templates/05_llm_as_judge_review_plan_template.md",
    "06_评测报告模板.md": "templates/06_evaluation_report_template.md",
    "07_BadCase分析报告模板.md": "templates/07_bad_case_analysis_report_template.md",
    "08_持续评测机制.md": "templates/08_continuous_evaluation_template.md",
}

DEFAULT_PACKAGE = [
    "01_评测方案.md",
    "02_Benchmark用例设计.md",
    "03_benchmark_cases.csv",
    "04_Rubric评分标准.md",
    "05_LLM-as-Judge评审方案.md",
    "07_bad_case_log.csv",
    "08_持续评测机制.md",
]


def headings(path: Path) -> List[str]:
    return [
        line.strip()[3:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]


def ordered_missing(required: Sequence[str], actual: Sequence[str]) -> List[str]:
    missing = [item for item in required if item not in actual]
    if missing:
        return missing
    positions = [actual.index(item) for item in required]
    if positions != sorted(positions):
        return ["<章节顺序与模板不一致>"]
    return []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_from_run(run_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (run_dir / path).resolve()


def validate_markdown(product_dir: Path, errors: List[str]) -> None:
    for output_name, template_name in TEMPLATE_MAP.items():
        output = product_dir / output_name
        if not output.exists():
            continue
        required = headings(ROOT / template_name)
        actual = headings(output)
        missing = ordered_missing(required, actual)
        if missing:
            errors.append("{} 缺少或错序章节：{}".format(output_name, "、".join(missing)))
        content = output.read_text(encoding="utf-8")
        if output_name == "05_LLM-as-Judge评审方案.md":
            for marker in ('"dimension_scores"', '"veto_candidate"', '"human_review"'):
                if marker not in content:
                    errors.append("{} 缺少 Judge JSON 字段 {}".format(output_name, marker))
            if "可复制 Judge Prompt" not in content:
                errors.append("{} 缺少可复制 Judge Prompt".format(output_name))


def validate_csvs(product_dir: Path, errors: List[str]) -> None:
    schemas = {
        "03_benchmark_cases.csv": BENCHMARK_FIELDS,
        "07_bad_case_log.csv": [
            "bad_case_id",
            "来源",
            "发现时间",
            "评测运行ID",
            "关联用例编号",
            "产品/模块",
            "模型版本/方案",
            "用户输入",
            "上下文/素材",
            "模型输出",
            "期望输出描述",
            "失败类型",
            "严重度",
            "风险等级",
            "是否触发一票否决",
            "一票否决规则",
            "根因层",
            "根因说明",
            "修复建议",
            "状态",
            "是否加入Benchmark",
            "新增/回归用例编号",
            "复测结果",
            "备注",
        ],
    }
    for name, expected in schemas.items():
        path = product_dir / name
        if not path.exists():
            continue
        try:
            fields, _ = read_csv(path)
            if fields != expected:
                errors.append("{} 字段或顺序与模板不一致".format(name))
        except RunnerError as exc:
            errors.append(str(exc))
    benchmark_path = product_dir / "03_benchmark_cases.csv"
    if benchmark_path.exists():
        try:
            _, raw_rows = read_csv(benchmark_path)
            if raw_rows:
                benchmark = read_benchmark(benchmark_path)
                validate_material_manifest(benchmark_path, benchmark)
        except (RunnerError, UnicodeDecodeError) as exc:
            errors.append(str(exc))


def validate_runner_config(product_dir: Path, errors: List[str]) -> None:
    path = product_dir / "runner_config.json"
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append("runner_config.json 无法解析：{}".format(exc))
        return
    if config.get("adapter") not in {"command", "http", "import"}:
        errors.append("runner_config.json adapter 无效")
    sensitive = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
    for key, value in config.get("headers", {}).items():
        if str(key).lower() in sensitive and not (
            isinstance(value, str) and value.startswith("${") and value.endswith("}")
        ):
            errors.append("runner_config.json 的敏感 Header {} 必须引用环境变量".format(key))


def validate_runs(product_dir: Path, errors: List[str]) -> None:
    runs = product_dir / "runs"
    if not runs.exists():
        return
    for run_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
        manifest_path = run_dir / "run.json"
        results_path = run_dir / "results.csv"
        if not manifest_path.exists() or not results_path.exists():
            errors.append("{} 缺少 run.json 或 results.csv".format(run_dir.name))
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append("{}/run.json 无法解析：{}".format(run_dir.name, exc))
            continue
        benchmark = resolve_from_run(run_dir, str(manifest.get("benchmark_file", "")))
        if not benchmark.exists():
            errors.append("{}/run.json 的 benchmark_file 不存在：{}".format(run_dir.name, benchmark))
        elif manifest.get("benchmark_sha256") and sha256(benchmark) != manifest["benchmark_sha256"]:
            errors.append("{}/run.json 的 Benchmark 摘要不匹配".format(run_dir.name))
        elif manifest.get("benchmark_bundle_sha256"):
            try:
                cases = read_benchmark(benchmark)
                if benchmark_bundle_sha256(benchmark, cases) != manifest["benchmark_bundle_sha256"]:
                    errors.append("{}/run.json 的 Benchmark Bundle 摘要不匹配".format(run_dir.name))
            except (RunnerError, UnicodeDecodeError) as exc:
                errors.append("{}/run.json 的 Benchmark Bundle 无法校验：{}".format(run_dir.name, exc))
        rubric_value = str(manifest.get("rubric_file", ""))
        if rubric_value:
            rubric = resolve_from_run(run_dir, rubric_value)
            if not rubric.exists():
                errors.append("{}/run.json 的 rubric_file 不存在：{}".format(run_dir.name, rubric))
        try:
            fields, rows = read_csv(results_path)
            if fields != RESULT_FIELDS:
                errors.append("{}/results.csv Schema 不一致".format(run_dir.name))
            if any(row.get("评测运行ID") != manifest.get("run_id") for row in rows):
                errors.append("{}/results.csv 含不匹配的评测运行ID".format(run_dir.name))
            counts = manifest.get("counts", {})
            actual = {
                "total": len(rows),
                "success": sum(row.get("候选执行状态") == "成功" for row in rows),
                "failed": sum(row.get("候选执行状态") == "失败" for row in rows),
            }
            actual["pending"] = actual["total"] - actual["success"] - actual["failed"]
            if counts and any(counts.get(key) != value for key, value in actual.items()):
                errors.append("{}/run.json counts 与 results.csv 不一致".format(run_dir.name))
        except RunnerError as exc:
            errors.append(str(exc))
        review = run_dir / "human_review.csv"
        if review.exists():
            try:
                fields, _ = read_csv(review)
                if fields != HUMAN_REVIEW_FIELDS:
                    errors.append("{}/human_review.csv Schema 不一致".format(run_dir.name))
            except RunnerError as exc:
                errors.append(str(exc))


def validate(product_dir: Path, require_default_package: bool) -> List[str]:
    errors: List[str] = []
    if not product_dir.is_dir():
        return ["产品目录不存在：{}".format(product_dir)]
    if require_default_package:
        missing = [name for name in DEFAULT_PACKAGE if not (product_dir / name).exists()]
        if missing:
            errors.append("默认资产包缺少：{}".format("、".join(missing)))
    validate_markdown(product_dir, errors)
    validate_csvs(product_dir, errors)
    validate_runner_config(product_dir, errors)
    validate_runs(product_dir, errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 AI 评测产物与模板、运行 Schema 的一致性")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--require-default-package", action="store_true")
    args = parser.parse_args()
    errors = validate(Path(args.product_dir).resolve(), args.require_default_package)
    if errors:
        print("输出校验失败：", file=sys.stderr)
        for error in errors:
            print("- {}".format(error), file=sys.stderr)
        return 2
    print("输出校验通过：{}".format(Path(args.product_dir).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

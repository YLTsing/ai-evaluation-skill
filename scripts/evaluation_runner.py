#!/usr/bin/env python3
"""Prepare and execute product evaluations from a stable Benchmark CSV."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from runner_common import (
    BENCHMARK_FIELDS,
    HUMAN_REVIEW_FIELDS,
    LEGACY_EXECUTION_MARKERS,
    RESULT_FIELDS,
    SCHEMA_VERSION,
    RunnerError,
    atomic_write_csv,
    atomic_write_json,
    benchmark_bundle_sha256,
    blank_result,
    dotted_get,
    load_json_records,
    make_run_id,
    now_iso,
    read_benchmark,
    read_csv,
    read_json,
    read_results,
    resolve_case_materials,
    sha256_file,
    slugify,
    stringify,
)


DEFAULT_MAPPING = {
    "output": "output",
    "model_latency_ms": "usage.model_latency_ms",
    "input_tokens": "usage.input_tokens",
    "output_tokens": "usage.output_tokens",
    "cost": "usage.cost",
    "currency": "usage.currency",
    "cost_source": "usage.cost_source",
    "trace_id": "trace_id",
}


def benchmark_payload(case: Mapping[str, str], run: Mapping[str, Any], sequence: int) -> Dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "case_id": case["用例编号"],
        "run_sequence": sequence,
        "input": case["输入"],
        "context": case["上下文/素材"],
    }


def resolve_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        resolved = os.environ.get(name)
        if resolved is None:
            raise RunnerError("缺少配置引用的环境变量：{}".format(name))
        return resolved
    if isinstance(value, dict):
        return {key: resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env(item) for item in value]
    return value


def call_command(config: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RunnerError("command 适配器要求 config.command 为非空参数数组。")
    timeout = float(config.get("timeout_seconds", 60))
    completed = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "无错误输出"
        raise RunnerError("产品命令退出码 {}：{}".format(completed.returncode, message[:1000]))
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError("产品命令必须向 stdout 返回 JSON：{}".format(exc)) from exc
    if not isinstance(response, dict):
        raise RunnerError("产品命令响应的 JSON 顶层必须是对象。")
    return response


def call_http(config: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    url = config.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RunnerError("http 适配器要求有效的 config.url。")
    raw_headers = config.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise RunnerError("config.headers 必须是对象。")
    sensitive = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
    for key, value in raw_headers.items():
        if str(key).lower() in sensitive and not (
            isinstance(value, str) and value.startswith("${") and value.endswith("}")
        ):
            raise RunnerError("敏感 HTTP Header {} 必须引用环境变量。".format(key))
    headers = resolve_env(raw_headers)
    headers = {str(key): str(value) for key, value in headers.items()}
    headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=str(config.get("method", "POST")).upper(),
    )
    timeout = float(config.get("timeout_seconds", 60))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RunnerError("产品 HTTP 响应 {}：{}".format(exc.code, body[:1000])) from exc
    except urllib.error.URLError as exc:
        raise RunnerError("产品 HTTP 调用失败：{}".format(exc.reason)) from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RunnerError("产品 HTTP 响应必须是 JSON：{}".format(exc)) from exc
    if not isinstance(parsed, dict):
        raise RunnerError("产品 HTTP 响应的 JSON 顶层必须是对象。")
    return parsed


def response_to_fields(response: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, str]:
    mapping = dict(DEFAULT_MAPPING)
    custom = config.get("response_mapping", {})
    if custom:
        if not isinstance(custom, dict):
            raise RunnerError("response_mapping 必须是对象。")
        mapping.update(custom)
    output = stringify(dotted_get(response, str(mapping["output"]))).strip()
    if not output:
        raise RunnerError("产品响应未包含非空模型输出，请检查 response_mapping.output。")
    cost = stringify(dotted_get(response, str(mapping["cost"])))
    source = stringify(dotted_get(response, str(mapping["cost_source"])))
    if not source:
        source = "runtime-reported" if cost else "unavailable"
    return {
        "模型输出": output,
        "模型调用耗时ms": stringify(dotted_get(response, str(mapping["model_latency_ms"]))),
        "输入Token": stringify(dotted_get(response, str(mapping["input_tokens"]))),
        "输出Token": stringify(dotted_get(response, str(mapping["output_tokens"]))),
        "费用": cost,
        "费用币种": stringify(dotted_get(response, str(mapping["currency"]))),
        "费用来源": source,
        "产品追踪ID": stringify(dotted_get(response, str(mapping["trace_id"]))),
    }


def load_run(run_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]], Path]:
    run = read_json(run_dir / "run.json")
    if run.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError("不支持的 run Schema：{}".format(run.get("schema_version")))
    benchmark_path = Path(str(run["benchmark_file"]))
    if not benchmark_path.is_absolute():
        benchmark_path = (run_dir / benchmark_path).resolve()
    benchmark = read_benchmark(benchmark_path)
    if sha256_file(benchmark_path) != run.get("benchmark_sha256"):
        raise RunnerError("Benchmark 在 run 初始化后发生变化，请创建新的 run。")
    if run.get("benchmark_bundle_sha256"):
        if benchmark_bundle_sha256(benchmark_path, benchmark) != run["benchmark_bundle_sha256"]:
            raise RunnerError("Benchmark Bundle 在 run 初始化后发生变化，请创建新的 run。")
    return run, benchmark, benchmark_path


def command_init(args: argparse.Namespace) -> None:
    benchmark_path = Path(args.benchmark).resolve()
    benchmark = read_benchmark(benchmark_path)
    bundle_sha256 = benchmark_bundle_sha256(benchmark_path, benchmark)
    material_manifest = benchmark_path.parent / "benchmark_materials" / "manifest.json"
    run_id = args.run_id or make_run_id(args.product_slug)
    run_root = Path(args.run_root).resolve()
    run_dir = run_root / run_id
    if run_dir.exists():
        raise RunnerError("run 目录已存在，拒绝覆盖：{}".format(run_dir))
    run_dir.mkdir(parents=True)
    results = []
    for case in benchmark:
        for sequence in range(1, args.runs_per_case + 1):
            results.append(blank_result(run_id, case["用例编号"], sequence))
    atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, results)
    relative_benchmark = os.path.relpath(str(benchmark_path), str(run_dir))
    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "prepared",
        "benchmark_file": relative_benchmark,
        "benchmark_sha256": sha256_file(benchmark_path),
        "benchmark_bundle_sha256": bundle_sha256,
        "material_manifest_file": (
            os.path.relpath(str(material_manifest), str(run_dir)) if material_manifest.exists() else ""
        ),
        "material_manifest_sha256": sha256_file(material_manifest) if material_manifest.exists() else "",
        "benchmark_version": args.benchmark_version,
        "product_slug": slugify(args.product_slug),
        "product_version": args.product_version,
        "candidate_model": args.candidate_model,
        "execution_adapter": args.adapter,
        "runs_per_case": args.runs_per_case,
        "rubric_file": args.rubric_file,
        "rubric_version": args.rubric_version,
        "judge_execution": args.judge_execution,
        "judge_model": args.judge_model,
        "created_at": now_iso(),
        "started_at": "",
        "finished_at": "",
        "config_sha256": "",
        "counts": {"total": len(results), "success": 0, "failed": 0, "pending": len(results)},
        "notes": args.notes,
    }
    atomic_write_json(run_dir / "run.json", run)
    print(str(run_dir))


def run_counts(rows: List[Dict[str, str]]) -> Dict[str, int]:
    success = sum(row["候选执行状态"] == "成功" for row in rows)
    failed = sum(row["候选执行状态"] == "失败" for row in rows)
    pending = len(rows) - success - failed
    return {"total": len(rows), "success": success, "failed": failed, "pending": pending}


def command_run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    run, benchmark, benchmark_path = load_run(run_dir)
    config = read_json(Path(args.config).resolve())
    adapter = str(config.get("adapter", run.get("execution_adapter", "")))
    if adapter not in {"command", "http"}:
        raise RunnerError("run 仅支持 command 或 http；导入请使用 import-results。")
    if run.get("execution_adapter") and adapter != run["execution_adapter"]:
        raise RunnerError("配置适配器与 run.json 不一致。")
    rows = read_results(run_dir / "results.csv")
    case_map = {case["用例编号"]: case for case in benchmark}
    config_bytes = json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    import hashlib

    run["config_sha256"] = hashlib.sha256(config_bytes).hexdigest()
    run["status"] = "running"
    run["started_at"] = run.get("started_at") or now_iso()
    atomic_write_json(run_dir / "run.json", run)
    max_retries = int(config.get("max_retries", 0))
    eligible = {"待执行"}
    if args.retry_failed:
        eligible.add("失败")
    for row in rows:
        if row["候选执行状态"] not in eligible:
            continue
        case = case_map.get(row["用例编号"])
        if not case:
            row["候选执行状态"] = "失败"
            row["候选错误信息"] = "Benchmark 中找不到用例。"
            atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, rows)
            continue
        resolved_case = resolve_case_materials(case, benchmark_path)
        payload = benchmark_payload(resolved_case, run, int(row["运行序号"]))
        error = ""
        started = time.monotonic()
        attempts = 0
        for attempt in range(max_retries + 1):
            attempts = attempt
            try:
                response = call_command(config, payload) if adapter == "command" else call_http(config, payload)
                values = response_to_fields(response, config)
                row.update(values)
                row["候选执行状态"] = "成功"
                row["候选错误信息"] = ""
                error = ""
                break
            except (RunnerError, subprocess.TimeoutExpired) as exc:
                error = "{}".format(exc)
                if attempt < max_retries:
                    time.sleep(float(config.get("retry_delay_seconds", 0)))
        row["端到端耗时ms"] = str(round((time.monotonic() - started) * 1000))
        row["重试次数"] = str(attempts)
        if error:
            row["候选执行状态"] = "失败"
            row["模型输出"] = ""
            row["候选错误信息"] = error[:2000]
        atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, rows)
    counts = run_counts(rows)
    if counts["success"] == counts["total"]:
        status = "completed"
    elif counts["success"]:
        status = "partial"
    else:
        status = "failed"
    run["status"] = status
    run["finished_at"] = now_iso()
    run["counts"] = counts
    atomic_write_json(run_dir / "run.json", run)
    print(json.dumps({"run_id": run["run_id"], "status": status, "counts": counts}, ensure_ascii=False))


def command_resume(args: argparse.Namespace) -> None:
    args.retry_failed = True
    command_run(args)


def command_import(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    run, _, _ = load_run(run_dir)
    rows = read_results(run_dir / "results.csv")
    records = load_json_records(args.input)
    index = {(row["用例编号"], row["运行序号"]): row for row in rows}
    updated = 0
    for record in records:
        case_id = str(record.get("case_id", ""))
        sequence = str(record.get("run_sequence", 1))
        row = index.get((case_id, sequence))
        if not row:
            raise RunnerError("导入记录无法匹配：{} / {}".format(case_id, sequence))
        fields = response_to_fields(record, {"response_mapping": record.get("response_mapping", {})})
        row.update(fields)
        row["候选执行状态"] = "成功"
        row["端到端耗时ms"] = stringify(record.get("end_to_end_latency_ms", ""))
        row["候选错误信息"] = ""
        updated += 1
    atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, rows)
    counts = run_counts(rows)
    run["status"] = "completed" if counts["success"] == counts["total"] else "partial"
    run["finished_at"] = now_iso()
    run["counts"] = counts
    run["import_source"] = args.source
    atomic_write_json(run_dir / "run.json", run)
    print(json.dumps({"updated": updated, "counts": counts}, ensure_ascii=False))


def legacy_result(row: Mapping[str, str], run_id: str, sequence: int) -> Dict[str, str]:
    result = {field: "" for field in RESULT_FIELDS}
    has_output = bool(row.get("模型输出", "").strip())
    has_judge = bool(row.get("Judge结果JSON", "").strip() or row.get("LLM-as-Judge 分数", "").strip())
    result.update(
        {
            "评测运行ID": run_id,
            "用例编号": row.get("用例编号", ""),
            "运行序号": row.get("运行序号", "") or str(sequence),
            "候选执行状态": "成功" if has_output else "待执行",
            "模型输出": row.get("模型输出", ""),
            "费用来源": "unavailable",
            "Judge执行状态": "成功" if has_judge else "待评审",
            "Judge分数JSON": row.get("Judge结果JSON", "") or row.get("LLM-as-Judge 分数", ""),
            "Judge评分理由": row.get("LLM-as-Judge 评分理由", ""),
            "Judge不确定性": row.get("Judge不确定性", ""),
            "一票否决候选": row.get("一票否决候选", ""),
            "一票否决候选理由": row.get("一票否决候选理由", ""),
            "是否进入人工复核": row.get("是否进入人工复核", ""),
            "人工复核原因": row.get("人工复核原因", ""),
        }
    )
    return result


def command_migrate(args: argparse.Namespace) -> None:
    source = Path(args.source).resolve()
    fields, legacy_rows = read_csv(source)
    if not LEGACY_EXECUTION_MARKERS.intersection(fields):
        raise RunnerError("源文件不是可识别的旧版宽表。")
    missing_design = [field for field in BENCHMARK_FIELDS if field not in fields]
    if missing_design:
        raise RunnerError("旧表缺少设计字段：{}".format("、".join(missing_design)))
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise RunnerError("迁移输出目录已存在，拒绝覆盖：{}".format(output))
    output.mkdir(parents=True)
    design_by_id: Dict[str, Dict[str, str]] = {}
    for row in legacy_rows:
        case_id = row.get("用例编号", "").strip()
        if not case_id:
            raise RunnerError("旧表存在空用例编号。")
        design = {field: row.get(field, "") for field in BENCHMARK_FIELDS}
        if case_id in design_by_id and design_by_id[case_id] != design:
            raise RunnerError("同一用例编号存在冲突设计字段：{}".format(case_id))
        design_by_id[case_id] = design
    benchmark_path = output / "03_benchmark_cases.csv"
    atomic_write_csv(benchmark_path, BENCHMARK_FIELDS, design_by_id.values())
    groups: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for row in legacy_rows:
        if not any(row.get(field, "").strip() for field in LEGACY_EXECUTION_MARKERS):
            continue
        legacy_id = row.get("评测运行ID", "").strip() or "legacy-import"
        version = row.get("模型版本/方案", "").strip() or "unknown"
        groups.setdefault((legacy_id, version), []).append(row)
    runs_root = output / "runs"
    for group_index, ((legacy_id, version), rows) in enumerate(groups.items(), start=1):
        run_id = "{}-{}-{}".format(slugify(legacy_id), slugify(version), group_index)
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        migrated_results = [legacy_result(row, run_id, index) for index, row in enumerate(rows, start=1)]
        atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, migrated_results)
        human_rows = []
        for source_row, result_row in zip(rows, migrated_results):
            if any(
                source_row.get(field, "").strip()
                for field in ("人工评测分数", "人工评测理由", "是否触发一票否决", "人工复核结果", "最终结论")
            ):
                human_rows.append(
                    {
                        "评测运行ID": run_id,
                        "用例编号": result_row["用例编号"],
                        "运行序号": result_row["运行序号"],
                        "人工复核原因": source_row.get("人工复核原因", "legacy-import"),
                        "人工评测分数": source_row.get("人工评测分数", ""),
                        "人工评测理由": source_row.get("人工评测理由", ""),
                        "是否确认一票否决": source_row.get("是否触发一票否决", ""),
                        "复核人": source_row.get("复核人", ""),
                        "复核时间": "",
                        "复核状态": source_row.get("复核状态", ""),
                        "人工复核结果": source_row.get("人工复核结果", ""),
                        "最终结论": source_row.get("最终结论", ""),
                    }
                )
        if human_rows:
            atomic_write_csv(run_dir / "human_review.csv", HUMAN_REVIEW_FIELDS, human_rows)
        counts = run_counts(migrated_results)
        run = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": "imported",
            "benchmark_file": os.path.relpath(str(benchmark_path), str(run_dir)),
            "benchmark_sha256": sha256_file(benchmark_path),
            "benchmark_version": "legacy-import",
            "product_slug": args.product_slug,
            "product_version": version,
            "candidate_model": version,
            "execution_adapter": "import",
            "runs_per_case": "unknown",
            "rubric_file": "",
            "rubric_version": "unknown",
            "judge_execution": "import",
            "judge_model": "unknown",
            "created_at": now_iso(),
            "started_at": "",
            "finished_at": now_iso(),
            "config_sha256": "",
            "counts": counts,
            "import_source": str(source),
            "notes": "由旧版宽表非破坏性迁移；请人工检查字段映射。",
        }
        atomic_write_json(run_dir / "run.json", run)
    print(json.dumps({"benchmark_cases": len(design_by_id), "runs": len(groups), "output": str(output)}, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 产品 Evaluation Runner")
    subparsers = parser.add_subparsers(dest="action", required=True)

    init = subparsers.add_parser("init", help="准备 run，不调用产品")
    init.add_argument("--benchmark", required=True)
    init.add_argument("--run-root", required=True)
    init.add_argument("--product-slug", required=True)
    init.add_argument("--product-version", required=True)
    init.add_argument("--candidate-model", default="")
    init.add_argument("--adapter", choices=["command", "http", "import"], required=True)
    init.add_argument("--runs-per-case", type=int, default=1)
    init.add_argument("--run-id")
    init.add_argument("--benchmark-version", default="")
    init.add_argument("--rubric-file", default="")
    init.add_argument("--rubric-version", default="")
    init.add_argument("--judge-execution", default="host-agent")
    init.add_argument("--judge-model", default="current-agent")
    init.add_argument("--notes", default="")
    init.set_defaults(func=command_init)

    run = subparsers.add_parser("run", help="执行待执行记录")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--retry-failed", action="store_true")
    run.set_defaults(func=command_run)

    resume = subparsers.add_parser("resume", help="继续待执行和失败记录，不覆盖成功记录")
    resume.add_argument("--run-dir", required=True)
    resume.add_argument("--config", required=True)
    resume.set_defaults(func=command_resume, retry_failed=True)

    import_parser = subparsers.add_parser(
        "import-results", aliases=["import"], help="导入真实产品结果"
    )
    import_parser.add_argument("--run-dir", required=True)
    import_parser.add_argument("--input", required=True, help="JSON、JSONL 或 -")
    import_parser.add_argument("--source", required=True)
    import_parser.set_defaults(func=command_import)

    migrate = subparsers.add_parser("migrate", help="非破坏性迁移旧版宽表")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--output-dir", required=True)
    migrate.add_argument("--product-slug", required=True)
    migrate.set_defaults(func=command_migrate)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if getattr(args, "runs_per_case", 1) < 1:
            raise RunnerError("runs-per-case 必须大于 0。")
        args.func(args)
        return 0
    except RunnerError as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

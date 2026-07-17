#!/usr/bin/env python3
"""Prepare host-agent Judge inputs and safely apply structured Judge results."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set, Tuple

from runner_common import (
    HUMAN_REVIEW_FIELDS,
    RESULT_FIELDS,
    SCHEMA_VERSION,
    RunnerError,
    atomic_write_csv,
    atomic_write_json,
    benchmark_bundle_sha256,
    load_json_records,
    now_iso,
    read_benchmark,
    read_csv,
    read_json,
    read_results,
    resolve_case_materials,
    sha256_file,
)


HUMAN_REVIEW_POLICY_VERSION = "v2.0"


def load_context(run_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]], Dict[str, Dict[str, str]]]:
    run = read_json(run_dir / "run.json")
    if run.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError("不支持的 run Schema：{}".format(run.get("schema_version")))
    benchmark_path = Path(str(run["benchmark_file"]))
    if not benchmark_path.is_absolute():
        benchmark_path = (run_dir / benchmark_path).resolve()
    benchmark = read_benchmark(benchmark_path)
    if run.get("benchmark_sha256") and sha256_file(benchmark_path) != run["benchmark_sha256"]:
        raise RunnerError("Benchmark 在 run 初始化后发生变化，请创建新的 run。")
    if run.get("benchmark_bundle_sha256"):
        if benchmark_bundle_sha256(benchmark_path, benchmark) != run["benchmark_bundle_sha256"]:
            raise RunnerError("Benchmark Bundle 在 run 初始化后发生变化，请创建新的 run。")
    resolved = [resolve_case_materials(row, benchmark_path) for row in benchmark]
    return run, read_results(run_dir / "results.csv"), {row["用例编号"]: row for row in resolved}


def command_prepare(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    run, rows, cases = load_context(run_dir)
    rubric_path = Path(args.rubric).resolve()
    plan_path = Path(args.review_plan).resolve()
    rubric = rubric_path.read_text(encoding="utf-8")
    review_plan = plan_path.read_text(encoding="utf-8")
    allowed_statuses = {"待评审"}
    if args.retry_failed:
        allowed_statuses.add("失败")
    selected = []
    for row in rows:
        if row["候选执行状态"] != "成功" or not row["模型输出"].strip():
            continue
        if row["Judge执行状态"] not in allowed_statuses:
            continue
        case = cases[row["用例编号"]]
        selected.append(
            {
                "case_id": row["用例编号"],
                "run_sequence": int(row["运行序号"]),
                "user_input": case["输入"],
                "context": case["上下文/素材"],
                "expected_output": case["预期输出描述"],
                "failure_modes": case["典型失败模式"],
                "candidate_output": row["模型输出"],
                "evaluation_dimensions": case["对应评测维度"],
                "priority": case["优先级"],
                "risk_level": case["风险等级"],
                "veto_rules": case["一票否决规则"],
                "human_review_rules": case["人工复核触发条件"],
            }
        )
        if args.limit and len(selected) >= args.limit:
            break
    if not selected:
        raise RunnerError("没有可评审的成功候选输出。")
    payload = {
        "run_id": run["run_id"],
        "judge_execution": "current-skill-invoking-agent",
        "judge_model": args.judge_model or run.get("judge_model", "current-agent"),
        "rubric_version": args.rubric_version or run.get("rubric_version", ""),
        "rubric": rubric,
        "review_plan": review_plan,
        "output_contract": {
            "results": [
                {
                    "case_id": "",
                    "run_sequence": 1,
                    "judge_model": "",
                    "dimension_scores": {
                        "dimension_name": {"score": 1, "reason": "", "evidence": ""}
                    },
                    "veto_candidate": {"candidate": False, "rules": [], "reason": ""},
                    "human_review": {"required": False, "reason": ""},
                    "uncertainty": "",
                    "preliminary_judgment": "",
                }
            ]
        },
        "cases": selected,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def validate_judge_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    case_id = str(record.get("case_id", "")).strip()
    if not case_id:
        raise RunnerError("Judge 结果缺少 case_id。")
    try:
        run_sequence = int(record.get("run_sequence", 1))
    except (TypeError, ValueError) as exc:
        raise RunnerError("{} 的 run_sequence 必须是整数。".format(case_id)) from exc
    scores = record.get("dimension_scores")
    if not isinstance(scores, dict) or not scores:
        raise RunnerError("{} 缺少 dimension_scores。".format(case_id))
    normalized_scores = {}
    for dimension, detail in scores.items():
        if not isinstance(detail, dict):
            raise RunnerError("{} / {} 的评分必须是对象。".format(case_id, dimension))
        score = detail.get("score")
        if score not in (1, 2, 3):
            raise RunnerError("{} / {} 的分数必须是 1、2 或 3。".format(case_id, dimension))
        reason = str(detail.get("reason", "")).strip()
        evidence = str(detail.get("evidence", "")).strip()
        if not reason or not evidence:
            raise RunnerError("{} / {} 必须同时提供 reason 和 evidence。".format(case_id, dimension))
        normalized_scores[str(dimension)] = {"score": score, "reason": reason, "evidence": evidence}
    veto = record.get("veto_candidate")
    if not isinstance(veto, dict) or not isinstance(veto.get("candidate"), bool):
        raise RunnerError("{} 的 veto_candidate.candidate 必须是布尔值。".format(case_id))
    rules = veto.get("rules", [])
    if not isinstance(rules, list):
        raise RunnerError("{} 的 veto_candidate.rules 必须是数组。".format(case_id))
    human = record.get("human_review")
    if not isinstance(human, dict) or not isinstance(human.get("required"), bool):
        raise RunnerError("{} 的 human_review.required 必须是布尔值。".format(case_id))
    preliminary = str(record.get("preliminary_judgment", "")).strip()
    if not preliminary:
        raise RunnerError("{} 缺少 preliminary_judgment。".format(case_id))
    return {
        "case_id": case_id,
        "run_sequence": run_sequence,
        "judge_model": str(record.get("judge_model", "current-agent")),
        "dimension_scores": normalized_scores,
        "veto_candidate": {
            "candidate": veto["candidate"],
            "rules": [str(rule) for rule in rules],
            "reason": str(veto.get("reason", "")),
        },
        "human_review": {
            "required": human["required"],
            "reason": str(human.get("reason", "")),
        },
        "uncertainty": str(record.get("uncertainty", "")),
        "preliminary_judgment": preliminary,
    }


def mandatory_reasons(case: Mapping[str, str], judged: Mapping[str, Any]) -> List[str]:
    reasons = []
    if any(detail["score"] == 1 for detail in judged["dimension_scores"].values()):
        reasons.append("存在 1 分维度")
    if judged["veto_candidate"]["candidate"]:
        reasons.append("一票否决候选")
    if judged["uncertainty"].strip():
        reasons.append("Judge 标记不确定")
    if judged["human_review"]["required"]:
        reasons.append(judged["human_review"]["reason"].strip() or "Judge 建议人工复核")
    return list(dict.fromkeys(reason for reason in reasons if reason))


def stable_sample(rows: List[Dict[str, str]], run_id: str, count: int) -> Set[Tuple[str, str]]:
    ranked = sorted(
        rows,
        key=lambda row: hashlib.sha256(
            "{}:{}:{}".format(run_id, row["用例编号"], row["运行序号"]).encode("utf-8")
        ).hexdigest(),
    )
    return {(row["用例编号"], row["运行序号"]) for row in ranked[:count]}


def stable_case_sample(
    rows: List[Dict[str, str]], run_id: str, count: int
) -> Set[Tuple[str, str]]:
    """Select at most one stable record per case so repeats do not crowd out coverage."""
    by_case: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        by_case.setdefault(row["用例编号"], []).append(row)
    ranked_cases = sorted(
        by_case,
        key=lambda case_id: hashlib.sha256(
            "{}:{}".format(run_id, case_id).encode("utf-8")
        ).hexdigest(),
    )
    selected: Set[Tuple[str, str]] = set()
    for case_id in ranked_cases[:count]:
        selected.update(stable_sample(by_case[case_id], run_id, 1))
    return selected


def is_priority_risk_case(case: Mapping[str, str]) -> bool:
    return case["优先级"].strip().upper() == "P0" or case["风险等级"].strip().lower() in {
        "高",
        "high",
        "p0",
    }


def score_profile(row: Mapping[str, str]) -> Tuple[Tuple[str, int], ...]:
    scores = json.loads(row["Judge分数JSON"])
    return tuple(sorted((str(name), int(detail["score"])) for name, detail in scores.items()))


def has_multi_run_variance(rows: List[Dict[str, str]]) -> bool:
    if len(rows) < 2:
        return False
    veto_states = {row["一票否决候选"] for row in rows}
    if len(veto_states) > 1:
        return True
    scores_by_dimension: Dict[str, List[int]] = {}
    for row in rows:
        for dimension, score in score_profile(row):
            scores_by_dimension.setdefault(dimension, []).append(score)
    return any(max(scores) - min(scores) >= 1 for scores in scores_by_dimension.values())


def rebuild_human_review(
    run_dir: Path,
    run_id: str,
    results: List[Dict[str, str]],
    cases: Mapping[str, Dict[str, str]],
) -> int:
    reasons_by_key: Dict[Tuple[str, str], List[str]] = {}
    judged_by_case: Dict[str, List[Dict[str, str]]] = {}
    high_risk_clean: Dict[str, List[Dict[str, str]]] = {}
    ordinary_by_type: Dict[str, List[Dict[str, str]]] = {}
    for row in results:
        if row["Judge执行状态"] != "成功":
            continue
        judged_by_case.setdefault(row["用例编号"], []).append(row)
        key = (row["用例编号"], row["运行序号"])
        forced_reasons = [
            reason
            for reason in row["人工复核原因"].split("；")
            if reason
            and not reason.startswith("高风险 case 代表复核")
            and not reason.startswith("普通样本分层抽样")
            and not reason.startswith("同一 case 多轮评分或否决状态异常")
            and not reason.startswith("本 Benchmark 为 P0/P1")
            and not reason.startswith("P0/P1 用例强制复核")
            and reason not in {"高优先级用例", "高风险用例"}
        ]
        if forced_reasons:
            reasons_by_key[key] = forced_reasons
        elif is_priority_risk_case(cases[row["用例编号"]]):
            high_risk_clean.setdefault(row["用例编号"], []).append(row)
        else:
            case_type = cases[row["用例编号"]]["用例类型"] or "未分类"
            ordinary_by_type.setdefault(case_type, []).append(row)

    for case_id, candidates in judged_by_case.items():
        if has_multi_run_variance(candidates):
            for row in candidates:
                key = (row["用例编号"], row["运行序号"])
                reasons_by_key.setdefault(key, []).append("同一 case 多轮评分或否决状态异常")

    for case_id, candidates in high_risk_clean.items():
        if has_multi_run_variance(judged_by_case[case_id]):
            continue
        for key in stable_sample(candidates, run_id, 1):
            reasons_by_key.setdefault(key, []).append("高风险 case 代表复核")

    for case_type, candidates in ordinary_by_type.items():
        case_count = len({row["用例编号"] for row in candidates})
        sample_count = min(case_count, max(2, math.ceil(case_count * 0.1)))
        for key in stable_case_sample(candidates, run_id, sample_count):
            reasons_by_key.setdefault(key, []).append("普通样本分层抽样：{}".format(case_type))

    for row in results:
        if row["Judge执行状态"] != "成功":
            continue
        key = (row["用例编号"], row["运行序号"])
        reasons = list(dict.fromkeys(reasons_by_key.get(key, [])))
        row["是否进入人工复核"] = "是" if reasons else "否"
        row["人工复核原因"] = "；".join(reasons)
    path = run_dir / "human_review.csv"
    existing: Dict[Tuple[str, str], Dict[str, str]] = {}
    if path.exists():
        fields, rows = read_csv(path)
        if fields != HUMAN_REVIEW_FIELDS:
            raise RunnerError("现有 human_review.csv Schema 不匹配，拒绝覆盖。")
        existing = {(row["用例编号"], row["运行序号"]): row for row in rows}
    output = []
    for key in sorted(reasons_by_key):
        row = existing.get(key, {field: "" for field in HUMAN_REVIEW_FIELDS})
        row["评测运行ID"] = run_id
        row["用例编号"], row["运行序号"] = key
        row["人工复核原因"] = "；".join(dict.fromkeys(reasons_by_key[key]))
        row["复核状态"] = row["复核状态"] or "待复核"
        output.append(row)
    if output:
        atomic_write_csv(path, HUMAN_REVIEW_FIELDS, output)
    return len(output)


def command_apply(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    run, rows, cases = load_context(run_dir)
    records = [validate_judge_record(record) for record in load_json_records(args.input)]
    index = {(row["用例编号"], row["运行序号"]): row for row in rows}
    updates = []
    for judged in records:
        key = (judged["case_id"], str(judged["run_sequence"]))
        row = index.get(key)
        if not row:
            raise RunnerError("Judge 结果无法匹配运行记录：{}".format(key))
        if row["候选执行状态"] != "成功" or not row["模型输出"].strip():
            raise RunnerError("Judge 只能回填执行成功且输出非空的记录：{}".format(key))
        case = cases[judged["case_id"]]
        reasons = mandatory_reasons(case, judged)
        updates.append((row, judged, reasons))
    for row, judged, reasons in updates:
        row["Judge执行状态"] = "成功"
        row["Judge分数JSON"] = json.dumps(
            judged["dimension_scores"], ensure_ascii=False, separators=(",", ":")
        )
        row["Judge评分理由"] = judged["preliminary_judgment"]
        row["Judge不确定性"] = judged["uncertainty"]
        row["一票否决候选"] = "是" if judged["veto_candidate"]["candidate"] else "否"
        row["一票否决候选理由"] = judged["veto_candidate"]["reason"]
        row["是否进入人工复核"] = "是" if reasons else "否"
        row["人工复核原因"] = "；".join(reasons)
    review_count = rebuild_human_review(run_dir, str(run["run_id"]), rows, cases)
    atomic_write_csv(run_dir / "results.csv", RESULT_FIELDS, rows)
    models = sorted({judged["judge_model"] for judged in records if judged["judge_model"]})
    if models:
        run["judge_model"] = ", ".join(models)
    run["judge_execution"] = "current-skill-invoking-agent"
    run["judge_updated_at"] = now_iso()
    run["human_review_policy_version"] = HUMAN_REVIEW_POLICY_VERSION
    run["judge_counts"] = {
        "eligible": sum(row["候选执行状态"] == "成功" and bool(row["模型输出"].strip()) for row in rows),
        "success": sum(row["Judge执行状态"] == "成功" for row in rows),
        "failed": sum(row["Judge执行状态"] == "失败" for row in rows),
        "pending": sum(
            row["候选执行状态"] == "成功" and row["Judge执行状态"] == "待评审" for row in rows
        ),
    }
    atomic_write_json(run_dir / "run.json", run)
    print(json.dumps({"applied": len(updates), "human_review_rows": review_count}, ensure_ascii=False))


def command_status(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    run, rows, _ = load_context(run_dir)
    review_path = run_dir / "human_review.csv"
    review_rows = []
    if review_path.exists():
        fields, review_rows = read_csv(review_path)
        if fields != HUMAN_REVIEW_FIELDS:
            raise RunnerError("human_review.csv Schema 不匹配。")
    required = len(review_rows)
    completed = sum(
        row["复核状态"] == "已完成"
        and bool(row["人工复核结果"].strip())
        and bool(row["最终结论"].strip())
        for row in review_rows
    )
    unresolved_veto = sum(
        row["是否确认一票否决"].strip() not in {"是", "否"}
        for row in review_rows
        if "一票否决候选" in row["人工复核原因"]
    )
    candidate_success = sum(row["候选执行状态"] == "成功" for row in rows)
    judge_success = sum(row["Judge执行状态"] == "成功" for row in rows)
    gate = (
        candidate_success == len(rows)
        and judge_success == candidate_success
        and completed == required
        and unresolved_veto == 0
    )
    summary = {
        "run_id": run["run_id"],
        "run_status": run["status"],
        "candidate_success": candidate_success,
        "candidate_total": len(rows),
        "judge_success": judge_success,
        "human_review_required": required,
        "human_review_completed": completed,
        "unresolved_veto_candidates": unresolved_veto,
        "formal_report_gate": "pass" if gate else "fail",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 产品 Judge Runner")
    subparsers = parser.add_subparsers(dest="action", required=True)

    prepare = subparsers.add_parser("prepare", help="为当前调用 Skill 的 Agent 准备 Judge 输入")
    prepare.add_argument("--run-dir", required=True)
    prepare.add_argument("--rubric", required=True)
    prepare.add_argument("--review-plan", required=True)
    prepare.add_argument("--rubric-version", default="")
    prepare.add_argument("--judge-model", default="current-agent")
    prepare.add_argument("--limit", type=int, default=0)
    prepare.add_argument("--retry-failed", action="store_true")
    prepare.set_defaults(func=command_prepare)

    apply_parser = subparsers.add_parser("apply", help="校验并回填 Judge 结构化结果")
    apply_parser.add_argument("--run-dir", required=True)
    apply_parser.add_argument("--input", required=True, help="JSON、JSONL 或 -")
    apply_parser.set_defaults(func=command_apply)

    status = subparsers.add_parser("status", help="检查 Judge 和正式报告门禁状态")
    status.add_argument("--run-dir", required=True)
    status.set_defaults(func=command_status)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (RunnerError, OSError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

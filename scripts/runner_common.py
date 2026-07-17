#!/usr/bin/env python3
"""Shared schemas and safe file operations for evaluation runners."""

from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCHEMA_VERSION = "2.0"
MATERIAL_MANIFEST_SCHEMA_VERSION = "1.0"
TEXT_MATERIAL_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb",
    ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".sql", ".html", ".css",
    ".xml", ".toml", ".ini", ".cfg", ".log",
}
MATERIAL_MEDIA_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
    ".jsonl": "application/x-ndjson", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".mp4": "video/mp4",
}

BENCHMARK_FIELDS = [
    "用例编号",
    "用例类型",
    "优先级",
    "对应评测维度",
    "输入",
    "上下文/素材",
    "预期输出描述",
    "典型失败模式",
    "风险等级",
    "一票否决规则",
    "人工复核触发条件",
    "备注",
]

RESULT_FIELDS = [
    "评测运行ID",
    "用例编号",
    "运行序号",
    "候选执行状态",
    "模型输出",
    "端到端耗时ms",
    "模型调用耗时ms",
    "输入Token",
    "输出Token",
    "费用",
    "费用币种",
    "费用来源",
    "产品追踪ID",
    "重试次数",
    "候选错误信息",
    "Judge执行状态",
    "Judge分数JSON",
    "Judge评分理由",
    "Judge不确定性",
    "一票否决候选",
    "一票否决候选理由",
    "是否进入人工复核",
    "人工复核原因",
]

HUMAN_REVIEW_FIELDS = [
    "评测运行ID",
    "用例编号",
    "运行序号",
    "人工复核原因",
    "人工评测分数",
    "人工评测理由",
    "是否确认一票否决",
    "复核人",
    "复核时间",
    "复核状态",
    "人工复核结果",
    "最终结论",
]

LEGACY_EXECUTION_MARKERS = {
    "模型版本/方案",
    "评测运行ID",
    "模型输出",
    "Judge结果JSON",
    "人工评测分数",
    "最终结论",
}


class RunnerError(Exception):
    """Raised for user-actionable runner validation errors."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    cleaned = []
    previous_dash = False
    for char in value.lower().strip():
        if char.isalnum():
            cleaned.append(char)
            previous_dash = False
        elif not previous_dash:
            cleaned.append("-")
            previous_dash = True
    result = "".join(cleaned).strip("-")
    return result or "product"


def make_run_id(product_slug: str) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    suffix = hashlib.sha256(os.urandom(16)).hexdigest()[:6]
    return "run-{}-{}-{}".format(timestamp, slugify(product_slug), suffix)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_material_path(benchmark_path: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if not raw_path.strip() or relative.is_absolute():
        raise RunnerError("素材路径必须是非空相对路径：{}".format(raw_path))
    root = benchmark_path.resolve().parent
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RunnerError("素材路径越出 Benchmark 目录：{}".format(raw_path)) from exc
    if not resolved.is_file():
        raise RunnerError("素材文件不存在：{}".format(raw_path))
    return resolved


def _reference_paths(value: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        if "$material_ref" in value:
            if set(value) != {"$material_ref"}:
                raise RunnerError("$material_ref 对象不能包含其他字段。")
            reference = value["$material_ref"]
            if not isinstance(reference, str):
                raise RunnerError("$material_ref 必须是相对路径字符串。")
            paths.append(reference)
        if "$material_refs" in value:
            if set(value) != {"$material_refs"}:
                raise RunnerError("$material_refs 对象不能包含其他字段。")
            references = value["$material_refs"]
            if not isinstance(references, dict) or not references:
                raise RunnerError("$material_refs 必须是非空对象。")
            for name, reference in references.items():
                if not str(name).strip() or not isinstance(reference, str):
                    raise RunnerError("$material_refs 的名称和路径必须是非空字符串。")
                paths.append(reference)
        for key, item in value.items():
            if key not in {"$material_ref", "$material_refs"}:
                paths.extend(_reference_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_reference_paths(item))
    return paths


def material_media_type(path: Path) -> str:
    return MATERIAL_MEDIA_TYPES.get(
        path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def parse_material_cell(cell: str) -> Any:
    stripped = cell.strip()
    if not stripped or stripped[0] not in "[{":
        return cell
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return cell
    return parsed if _reference_paths(parsed) else cell


def referenced_materials(benchmark_path: Path, benchmark: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    entries: Dict[str, Dict[str, str]] = {}
    for case in benchmark:
        case_id = case["用例编号"].strip()
        for role, field in (("input", "输入"), ("context", "上下文/素材")):
            parsed = parse_material_cell(case[field])
            for raw_path in _reference_paths(parsed):
                path = _safe_material_path(benchmark_path, raw_path)
                relative = path.relative_to(benchmark_path.resolve().parent).as_posix()
                entry = {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "media_type": material_media_type(path),
                }
                entries[relative] = entry
    return [entries[key] for key in sorted(entries)]


def validate_material_manifest(
    benchmark_path: Path,
    benchmark: Sequence[Mapping[str, str]],
    require_when_referenced: bool = True,
) -> Dict[str, Any]:
    entries = referenced_materials(benchmark_path, benchmark)
    manifest_path = benchmark_path.resolve().parent / "benchmark_materials" / "manifest.json"
    if not entries:
        if manifest_path.exists():
            raise RunnerError("存在素材 Manifest，但 Benchmark 没有引用任何素材。")
        return {"entries": [], "manifest_path": None, "manifest_sha256": ""}
    if require_when_referenced and not manifest_path.is_file():
        raise RunnerError("Benchmark 引用了素材，但缺少 benchmark_materials/manifest.json。")
    if not manifest_path.is_file():
        return {"entries": entries, "manifest_path": None, "manifest_sha256": ""}
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != MATERIAL_MANIFEST_SCHEMA_VERSION:
        raise RunnerError("素材 Manifest schema_version 必须为 {}。".format(MATERIAL_MANIFEST_SCHEMA_VERSION))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RunnerError("素材 Manifest files 必须是数组。")
    manifest_entries: Dict[str, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RunnerError("素材 Manifest 的每个 files 项必须包含字符串 path。")
        relative = Path(item["path"]).as_posix()
        if relative in manifest_entries:
            raise RunnerError("素材 Manifest 存在重复路径：{}".format(relative))
        manifest_entries[relative] = item
    expected = {entry["path"]: entry for entry in entries}
    missing = sorted(set(expected) - set(manifest_entries))
    extra = sorted(set(manifest_entries) - set(expected))
    if missing or extra:
        raise RunnerError(
            "素材 Manifest 与 Benchmark 引用不一致；缺少：{}；孤立：{}".format(
                "、".join(missing) or "无", "、".join(extra) or "无"
            )
        )
    for relative, actual in expected.items():
        declared = manifest_entries[relative]
        if declared.get("sha256") != actual["sha256"]:
            raise RunnerError("素材 Manifest 摘要不匹配：{}".format(relative))
        if declared.get("media_type") and declared.get("media_type") != actual["media_type"]:
            raise RunnerError("素材 Manifest media_type 不匹配：{}".format(relative))
    return {
        "entries": entries,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
    }


def benchmark_bundle_sha256(benchmark_path: Path, benchmark: Sequence[Mapping[str, str]]) -> str:
    material = validate_material_manifest(benchmark_path, benchmark)
    digest = hashlib.sha256()
    digest.update("benchmark\0{}\0{}".format(benchmark_path.name, sha256_file(benchmark_path)).encode("utf-8"))
    if material["manifest_path"]:
        digest.update("manifest\0{}".format(material["manifest_sha256"]).encode("utf-8"))
    for entry in material["entries"]:
        digest.update("file\0{}\0{}".format(entry["path"], entry["sha256"]).encode("utf-8"))
    return digest.hexdigest()


def _resolve_material_value(value: Any, benchmark_path: Path) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$material_ref"}:
            path = _safe_material_path(benchmark_path, value["$material_ref"])
            if path.suffix.lower() in TEXT_MATERIAL_SUFFIXES:
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise RunnerError("文本素材不是有效 UTF-8：{}".format(value["$material_ref"])) from exc
                if not content.strip():
                    raise RunnerError("文本素材为空：{}".format(value["$material_ref"]))
                return content
            return {
                "type": "file",
                "path": str(path),
                "media_type": material_media_type(path),
                "sha256": sha256_file(path),
            }
        if set(value) == {"$material_refs"}:
            references = value["$material_refs"]
            return {
                str(name): _resolve_material_value({"$material_ref": path}, benchmark_path)
                for name, path in references.items()
            }
        return {key: _resolve_material_value(item, benchmark_path) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_material_value(item, benchmark_path) for item in value]
    return value


def resolve_material_cell(cell: str, benchmark_path: Path) -> str:
    parsed = parse_material_cell(cell)
    if isinstance(parsed, str):
        return parsed
    resolved = _resolve_material_value(parsed, benchmark_path)
    if isinstance(resolved, str):
        return resolved
    return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))


def resolve_case_materials(case: Mapping[str, str], benchmark_path: Path) -> Dict[str, str]:
    return {
        **dict(case),
        "输入": resolve_material_cell(case["输入"], benchmark_path),
        "上下文/素材": resolve_material_cell(case["上下文/素材"], benchmark_path),
    }


def read_csv(path: Path) -> tuple[List[str], List[Dict[str, str]]]:
    if not path.exists():
        raise RunnerError("文件不存在：{}".format(path))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise RunnerError("CSV 存在重复字段：{}".format(path))
        return fields, [dict(row) for row in reader]


def validate_fields(actual: Sequence[str], expected: Sequence[str], label: str) -> None:
    missing = [field for field in expected if field not in actual]
    extra = [field for field in actual if field not in expected]
    if missing or extra:
        details = []
        if missing:
            details.append("缺少字段：{}".format("、".join(missing)))
        if extra:
            details.append("多余字段：{}".format("、".join(extra)))
        raise RunnerError("{} Schema 不匹配；{}".format(label, "；".join(details)))


def read_benchmark(path: Path) -> List[Dict[str, str]]:
    fields, rows = read_csv(path)
    if LEGACY_EXECUTION_MARKERS.intersection(fields):
        raise RunnerError(
            "检测到旧版宽表 Benchmark。请先运行 evaluation_runner.py migrate，"
            "Runner 不会直接改写旧数据。"
        )
    validate_fields(fields, BENCHMARK_FIELDS, "Benchmark")
    if not rows:
        raise RunnerError("Benchmark 只有表头，没有可执行用例。")
    case_ids = [row["用例编号"].strip() for row in rows]
    if any(not case_id for case_id in case_ids):
        raise RunnerError("Benchmark 存在空用例编号。")
    if len(case_ids) != len(set(case_ids)):
        raise RunnerError("Benchmark 用例编号不唯一。")
    return rows


def atomic_write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False, dir=str(path.parent)
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
        temp_name = handle.name
    os.replace(temp_name, path)


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise RunnerError("文件不存在：{}".format(path))
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RunnerError("JSON 无法解析：{} ({})".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise RunnerError("JSON 顶层必须是对象：{}".format(path))
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent)
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def blank_result(run_id: str, case_id: str, sequence: int) -> Dict[str, str]:
    row = {field: "" for field in RESULT_FIELDS}
    row.update(
        {
            "评测运行ID": run_id,
            "用例编号": case_id,
            "运行序号": str(sequence),
            "候选执行状态": "待执行",
            "费用来源": "unavailable",
            "Judge执行状态": "待评审",
        }
    )
    return row


def read_results(path: Path) -> List[Dict[str, str]]:
    fields, rows = read_csv(path)
    validate_fields(fields, RESULT_FIELDS, "运行结果")
    seen = set()
    for row in rows:
        key = (row["评测运行ID"], row["用例编号"], row["运行序号"])
        if key in seen:
            raise RunnerError("运行结果存在重复关联键：{}".format(key))
        seen.add(key)
    return rows


def dotted_get(value: Any, path: str, default: Any = "") -> Any:
    if not path:
        return default
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def load_json_records(path_or_dash: str) -> List[Dict[str, Any]]:
    import sys

    if path_or_dash == "-":
        content = sys.stdin.read()
    else:
        content = Path(path_or_dash).read_text(encoding="utf-8")
    content = content.strip()
    if not content:
        raise RunnerError("没有可读取的 JSON 记录。")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            records = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            records = parsed["results"]
        elif isinstance(parsed, dict):
            records = [parsed]
        else:
            raise RunnerError("JSON 必须是对象、对象数组或包含 results 数组的对象。")
    except json.JSONDecodeError:
        records = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunnerError("JSONL 第 {} 行无法解析：{}".format(line_number, exc)) from exc
            records.append(item)
    if not all(isinstance(record, dict) for record in records):
        raise RunnerError("每条 JSON 记录必须是对象。")
    return records

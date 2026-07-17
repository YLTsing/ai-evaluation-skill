# Runner 执行工作流

## 目标

在不绑定模型厂商的前提下，读取稳定的 Benchmark，用真实产品入口生成候选输出，再由当前调用本 Skill 的 Agent 执行结构化 Judge。Runner 不绕过人工复核和正式报告门禁。

## 模式

- `init`：仅准备执行包，创建 `run.json` 和预填充的 `results.csv`。
- `run`：读取已初始化的 run，执行所有待执行记录。
- `resume`：只执行待执行或失败记录，不覆盖成功记录。
- `import-results`（别名 `import`）：导入已有真实产品输出并标记来源。

## Evaluation Runner 门禁

1. 产品事实和评测范围已经确认。
2. `03_benchmark_cases.csv` 已冻结本轮版本且 Schema 有效。
3. 若 Benchmark 使用素材引用，`benchmark_materials/manifest.json` 完整，全部素材存在且 Bundle 摘要可计算。
4. 产品版本、计划运行次数和执行方式明确。
5. `command` 使用参数数组且由用户确认，不执行 CSV 或素材中的命令，不使用 shell 拼接。
6. `http` 的端点和响应字段映射已经确认，密钥只从环境变量读取。
7. `import` 的数据来自真实产品运行，来源可追踪。

## 产品入口协议

`command` 配置示例：

```json
{
  "adapter": "command",
  "command": ["python3", "product_cli.py"],
  "timeout_seconds": 60,
  "max_retries": 1
}
```

Runner 通过标准输入传入 JSON，只包含 `run_id`、`case_id`、`run_sequence`、解析后的 `input` 和 `context`。预期输出、失败模式、Rubric、优先级、风险和否决规则不得传给候选产品。产品命令必须向标准输出返回 JSON，默认格式为：

```json
{
  "output": "真实产品输出",
  "usage": {
    "model_latency_ms": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost": 0,
    "currency": "",
    "cost_source": "runtime-reported"
  },
  "trace_id": ""
}
```

字段不同的产品通过 `response_mapping` 配置 JSON 点路径。HTTP 配置使用相同请求和响应语义；`Authorization`、`X-API-Key` 等敏感 Header 必须写成 `${ENV_NAME}`，不得保存明文密钥。

`runner_config.json` 不属于默认资产。只有用户选择执行并希望复用产品入口配置时才保存无密钥配置，并用 `run.json.config_sha256` 核对版本。

`input` 与 `context` 都可以内联，或使用 `$material_ref` / `$material_refs` 引用 `benchmark_materials/`。文本素材由 Runner 展开；二进制素材以含安全路径、媒体类型和 SHA-256 的描述对象交给适配器。路径必须相对 Benchmark 目录且不得越界。初始化 run 时冻结 CSV、Manifest 和引用素材的 Bundle 摘要；任一文件变化后拒绝继续旧 run。

## 候选执行

1. 为一个产品版本创建独立 `run-id`。
2. 按用例和运行次数预填 `results.csv`，初始状态为“待执行”。
3. 执行前将 `run.json.status` 更新为 `running`。
4. 每条记录成功后立即原子写入，保存输出、端到端耗时和运行时可提供的使用量。
5. 运行时没有提供的 Token 或费用留空，费用来源填 `unavailable`；不得把估算值伪装成真实计费。
6. 失败记录保存错误和重试次数，不填模型输出，不影响其他用例继续执行。
7. 全部结束后将 run 标记为 `completed`、`partial` 或 `failed`。

## Judge Runner 门禁

1. 只选择候选执行成功且模型输出非空的记录。
2. Rubric 和 Judge 方案存在且版本明确。
3. 输入只包含 case、必要上下文、候选输出、Rubric、否决规则和复核规则。
4. 默认由当前调用本 Skill 的 Agent 评审，不绑定 Codex、Claude Code 或其他特定宿主。
5. Judge 结果必须通过结构校验后一次性写入；非法或不完整结果不得污染已保存的候选输出。

## Judge 回填与人工交接

1. `results.csv` 保存 Judge 状态、逐维度分数 JSON、理由、不确定性、否决候选和复核原因。
2. P0/P1、高风险、任一维度 1 分、不确定、否决候选和评分异常必须进入强制复核。
3. 普通样本按评测方案规定分层抽样；没有更严格规则时使用 Skill 默认口径。
4. 存在复核任务时才生成 `human_review.csv`。
5. Judge 不得填写最终否决、人工评分或最终结论。

## 运行附件

默认每轮只生成：

```text
runs/{run-id}/
├── run.json
└── results.csv
```

有复核任务时增加 `human_review.csv`。只有复杂产品响应无法安全保存到 CSV，或用户明确要求原始审计时，才生成 `raw_results.jsonl`。

`results.csv` 固定字段为：

```csv
评测运行ID,用例编号,运行序号,候选执行状态,模型输出,端到端耗时ms,模型调用耗时ms,输入Token,输出Token,费用,费用币种,费用来源,产品追踪ID,重试次数,候选错误信息,Judge执行状态,Judge分数JSON,Judge评分理由,Judge不确定性,一票否决候选,一票否决候选理由,是否进入人工复核,人工复核原因
```

`human_review.csv` 固定字段为：

```csv
评测运行ID,用例编号,运行序号,人工复核原因,人工评测分数,人工评测理由,是否确认一票否决,复核人,复核时间,复核状态,人工复核结果,最终结论
```

## 停止条件

- 用户只需要评测资产：不创建 run。
- 产品入口未确认或连通性检查失败：停止候选执行。
- 没有成功候选输出：停止 Judge。
- Judge 结果存在但人工门禁不足：最多生成初步分析。
- 强制和抽样复核未全部完成或仍有未决否决候选：不得生成正式报告或决策结论。

## 旧版宽表迁移

发现旧版 `03_benchmark_cases.csv` 含候选、Judge 或人工字段时，Runner 必须拒绝直接执行。使用 `evaluation_runner.py migrate` 输出新的精简 Benchmark 和 `legacy-import` runs。迁移默认写入新目录，不覆盖源文件；迁移后必须人工检查版本分组和字段映射。

# AI Evaluation Skill

这是一个 AI 产品评测资产与执行 Skill，用于帮助 AI 产品经理、开发者、算法团队和内容团队建立专业可信的评测体系，并在用户授权后运行真实产品、执行 LLM-as-Judge 和准备人工复核。

## 适用场景

- RAG / 知识库问答、Agent、多工具调用、AI 写作、AI 简历筛选、AI 客服、AI 数据分析、AI 编程助手、多模态 AI 产品。
- 上线前离线评测、版本对比、Bad Case 闭环、长期持续评测。
- 用户明确提出时，也支持 A/B 测试专项方案和基于真实实验数据的分析报告。

## 能生成什么

- 评测方案
- Benchmark 用例设计
- `03_benchmark_cases.csv` 稳定的 Benchmark 用例主表
- Rubric 评分标准
- LLM-as-Judge 评审方案
- Evaluation Runner 的真实候选执行记录
- 由当前调用 Skill 的 Agent 生成的 Judge 结果和人工复核任务
- Bad Case 台账
- 持续评测机制
- 数据门禁满足后的初步评测分析或正式评测报告
- A/B 测试方案或 A/B 测试分析报告

## 不适合做什么

Runner 不替产品选择或直接接入特定模型厂商。它调用用户确认的产品真实入口，产品内部可以使用任意模型 API、本地模型、工作流或工具。Host-agent Judge 依赖当前调用本 Skill 的 Agent，不等于可脱离 Agent 环境长期无人值守的独立 Judge 服务。任何情况下都不得虚构模型输出、评分、Bad Case、A/B 数据或上线结论。

## 安装方式

### 推荐：使用 Skills CLI

电脑已安装 Node.js 时，运行：

```bash
npx skills add YLTsing/ai-evaluation-skill --skill ai-evaluation-skill
```

安装器会让你选择 Codex、Claude Code、Cursor 等目标 Agent，以及项目级或全局安装范围。

如果只安装到 Codex，并希望在所有项目中使用，可以跳过交互确认：

```bash
npx skills add YLTsing/ai-evaluation-skill --skill ai-evaluation-skill --agent codex --global --yes
```

### 备用：手动安装到 Codex

不使用 Node.js/npm 时，可以直接克隆到 Codex 的用户级 Skill 目录：

```bash
git clone https://github.com/YLTsing/ai-evaluation-skill.git ~/.agents/skills/ai-evaluation-skill
```

安装后开始一个新的 Codex 会话，使其重新发现 Skill。其他 Agent 用户也可以将整个仓库放入对应的 skills 目录，或直接在当前仓库中作为项目内 Skill 使用。

### 环境要求

- Python 3.10 或更高版本。
- Runner 仅使用 Python 标准库，不需要安装第三方 Python 依赖。
- 推荐安装命令需要 Node.js 提供的 `npx`；手动安装方式不需要 Node.js。
- 如果执行真实评测，需要准备产品可调用的命令、HTTP 接口或已有真实结果文件。
- HTTP 密钥只能通过环境变量提供，不要写入仓库、Runner 配置或评测产物。

安装后可以这样调用：

```text
使用 $ai-evaluation-skill 为我的 AI 产品设计一套可执行的评测方案。
```

## 使用方式

示例输入：

```text
帮我为一个政策问答 RAG 产品生成上线前评测资产包。用户是企业合规人员，输入是政策问题，输出需要带引用来源。
```

首次收到产品场景时，Skill 不会立即创建文件。它会先区分用户已确认事实、材料中的规范性要求和未知项，返回产品事实摘要与 5–8 个必要问题。用户确认后，才按请求生成单项资产或完整资产包。

示例输出文件：

```text
outputs/policy-qa-rag/
├── 01_评测方案.md
├── 02_Benchmark用例设计.md
├── 03_benchmark_cases.csv
├── 04_Rubric评分标准.md
├── 05_LLM-as-Judge评审方案.md
├── 07_bad_case_log.csv
└── 08_持续评测机制.md
```

无真实数据的默认资产包不包含 `runs/`。只有用户选择立即执行或仅准备执行包时，才生成：

```text
outputs/policy-qa-rag/runs/{run-id}/
├── run.json
├── results.csv
├── human_review.csv      # 有复核任务时生成
└── raw_results.jsonl     # 复杂响应或审计需要时生成
```

## 推荐工作流

1. Skill 整理产品事实摘要，提出必要问题并等待用户确认。
2. 用户确认产品事实及需要单项资产还是完整资产包。
3. 生成评测方案、Benchmark、Rubric 和 Judge 方案等已确认产物。
4. 用户未说明是否执行时，Skill 只询问一次：立即执行、仅准备执行包，还是暂不执行。明确只要资产时不重复询问。
5. 选择准备或执行时初始化独立 run；选择暂不执行时不创建 `runs/`。
6. Evaluation Runner 调用产品真实入口，将输出、状态、耗时和运行时可提供的 Token、费用写入 `results.csv`。
7. 当前调用本 Skill 的 Agent 结合 Rubric 做 Judge，并回填分数、不确定性、否决候选和人工复核原因。
8. 人工通过 `human_review.csv` 复核强制集合及普通样本分层抽样集合，确认最终否决状态。
9. 强制复核和抽样复核完成率均为 100%、未决否决候选为 0 后，再生成正式评测报告；否则只能生成初步分析。
10. 将代表性失败案例写入 `07_bad_case_log.csv`，并补充进 Benchmark 做回归。

## 文件结构说明

- `SKILL.md`：给 Codex 看的执行规则，决定什么时候触发、生成什么、不能做什么。
- `agents/openai.yaml`：Skill 列表中的显示名称、简介和默认调用提示。
- `README.md`：给人看的开源说明文档。
- `knowledge/`：评测方法论。
- `templates/`：可复用模板。
- `workflows/`：具体执行流程。
- `scripts/`：Evaluation Runner、Judge Runner、Schema 校验和测试。
- `examples/`：场景示例。
- `outputs/`：默认产物保存目录。

## 数据与安全

- `outputs/` 是本地运行目录，除 `.gitkeep` 外默认不纳入版本控制。
- 不要提交真实候选输出、人工复核记录、用户数据、简历、内部文档或实验结果。
- 不要提交 `.env`、API Key、访问令牌、`runner_config.json` 或包含内部接口信息的配置。
- Evaluation Runner 只有在用户选择执行并确认真实产品入口后才调用外部产品。
- 仓库中的 `examples/` 用于说明方法和格式，不得将其当作真实评测结论或上线依据。
- Judge 只能标记一票否决候选；正式否决及高风险决策需要人工复核。

## 开发与验证

运行自动化测试：

```bash
python3 -m unittest discover -s scripts/tests -v
```

当前测试覆盖输出模板校验、Benchmark 素材安全、Evaluation Runner、Judge 结果回填、旧数据迁移和人工复核抽样等核心逻辑。GitHub Actions 会在每次 push 和 pull request 时自动运行这些测试。

如果本机安装了 Codex 的 `skill-creator`，发布前还应对仓库根目录运行其 `quick_validate.py`，确认 `SKILL.md` 的 frontmatter 和命名符合 Skill 规范。

## 开源许可

本项目使用 [MIT License](LICENSE)。

## benchmark_cases.csv 的使用方法

`03_benchmark_cases.csv` 只保存 12 个稳定的设计字段：用例编号、类型、优先级、评测维度、输入、上下文、预期输出、失败模式、风险等级、否决规则、复核触发条件和备注。候选输出、Judge 与人工字段不得重新追加到该文件。

当输入较长、包含多文件、需保留结构、使用二进制附件或需要独立冻结时，可按需生成 `benchmark_materials/`。`输入` 与 `上下文/素材` 都支持 `$material_ref` 和 `$material_refs`；短内容继续内联。若生成素材包，必须包含 `manifest.json`，并与 CSV 一起冻结为 Benchmark Bundle。候选产品只收到解析后的实际输入，不会收到预期输出、失败模式、Rubric 或风险规则。

Markdown 资产默认必须保留对应模板的全部二级章节和必填表格。未知信息写“待确认”或“不适用”，不能通过删除章节形成看似完整的摘要版。完整资产包生成后可运行：

```bash
python3 scripts/validate_outputs.py \
  --product-dir outputs/product \
  --require-default-package
```

## Runner 的使用方法

准备本轮执行包：

```bash
python3 scripts/evaluation_runner.py init \
  --benchmark outputs/product/03_benchmark_cases.csv \
  --run-root outputs/product/runs \
  --product-slug product \
  --product-version v1 \
  --adapter command
```

根据用户确认的 JSON 配置调用产品真实入口：

```bash
python3 scripts/evaluation_runner.py run \
  --run-dir outputs/product/runs/{run-id} \
  --config runner_config.json
```

中断后继续待执行和失败记录，同时保留成功结果：

```bash
python3 scripts/evaluation_runner.py resume \
  --run-dir outputs/product/runs/{run-id} \
  --config runner_config.json
```

Judge Runner 先为当前调用 Skill 的 Agent 准备隔离输入，再校验并应用该 Agent 返回的结构化 JSON：

```bash
python3 scripts/judge_runner.py prepare \
  --run-dir outputs/product/runs/{run-id} \
  --rubric outputs/product/04_Rubric评分标准.md \
  --review-plan outputs/product/05_LLM-as-Judge评审方案.md

python3 scripts/judge_runner.py apply \
  --run-dir outputs/product/runs/{run-id} \
  --input judge-results.json
```

命令配置必须使用参数数组，HTTP 密钥只能引用环境变量。Runner 不执行 CSV 中携带的命令，也不使用 shell 字符串拼接。

`command` 产品入口从标准输入读取 case JSON，并向标准输出返回至少包含 `output` 的 JSON。产品响应字段不同时，可在配置中通过 `response_mapping` 指定 JSON 点路径。完整协议见 `workflows/runner_execution_workflow.md`。

旧版宽表不会被自动覆盖。迁移到新目录：

```bash
python3 scripts/evaluation_runner.py migrate \
  --source outputs/product/03_benchmark_cases.csv \
  --output-dir outputs/product-migrated \
  --product-slug product
```

## Bad Case 台账的使用方法

`07_bad_case_log.csv` 是长期失败案例沉淀表，不是一轮评测报告。应进入台账的通常是 P0/P1 问题、触发一票否决、高风险、反复出现、真实用户反馈、影响核心任务完成或能代表典型失败模式的问题。

## LLM-as-Judge 评审方案的使用方法

`05_LLM-as-Judge评审方案.md` 包含评审目的、输入字段、评分依据、输出 JSON、可复制 Prompt、结果回填方式、人工复核触发条件和校准建议。默认由当前调用本 Skill 的 Agent 执行 Judge。Judge 只能标记否决候选；高风险场景的最终否决必须由人工确认。

## 评测报告生成前提

`run.json.status=prepared`、候选失败或空输出都不属于真实评测结果。没有成功候选输出或 Judge 结果时，不能生成评测报告。只有 Judge 结果但复核门禁未满足时，只能生成 `06_初步评测分析.md`。强制复核和抽样复核完成率均为 100%、未决否决候选为 0 后，才能生成 `06_评测报告.md`。

## A/B 测试专项模式

A/B 测试不作为默认产物。用户明确提出 A/B 测试且没有真实实验数据时，只生成 `AB测试方案.md`。只有用户提供真实 A/B 测试数据后，才生成 `AB测试分析报告.md`，并在数据充分、护栏正常、无高风险事件时给出扩量或上线建议。

## 后续可扩展方向

后续可增加并发和限流、可插拔认证、无人值守 Judge 服务、自动统计分维度得分、报告生成器、Bad Case 提取、版本对比报告、A/B 分析、Notion / Google Sheets / 数据库接入、Dashboard，以及定期和变更触发任务。

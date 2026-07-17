# Benchmark 执行闭环

## 目标

让 `03_benchmark_cases.csv` 保持为稳定的用例主表，并通过独立 run 记录候选执行、Judge 和人工裁决。

## 流程

1. 确认产品事实与评测目标已经过用户确认；知识库、PRD 或 Prompt 中的规范要求不得替代产品事实确认。
2. 生成 Benchmark 设计。
3. 生成 `03_benchmark_cases.csv`，区分用户直接任务的 `输入` 与支撑任务的 `上下文/素材`。
4. 对短且清晰的内容直接内联；对长文本、多文件、需保留结构、二进制文件或需复用/冻结的输入生成 `benchmark_materials/` 和 `manifest.json`。两个字段都允许引用素材。
5. 设计阶段只填充 12 个用例和规则字段，不加入任何运行列。
6. 用户选择准备或执行时，为一个产品版本初始化独立 `run-id`。
7. `run.json` 记录 CSV、Manifest 和引用素材组成的 Bundle 摘要，以及本轮共享版本、执行方式、运行次数和状态。
8. `results.csv` 按“一条 case × 一次运行一行”预填评测运行 ID、用例编号、运行序号和待执行状态。
9. Evaluation Runner 安全展开素材，只把运行关联字段、`input` 和 `context` 交给产品真实入口；不得发送预期输出、失败模式、Rubric 或风险规则。
10. Evaluation Runner 将真实输出、耗时、Token、费用来源、错误和追踪 ID 写入结果表。
11. 仅将执行成功且输出非空的记录交给当前调用本 Skill 的 Agent 执行 Judge。
12. Judge Runner 将解析后的输入、上下文、产品输出、预期行为、典型失败模式和 Rubric 组成评审输入。
13. Judge Runner 校验结构化结果后，写入分数、理由、不确定性、否决候选和复核原因。
14. Judge 不得填写最终否决或人工字段；有复核任务时生成 `human_review.csv`。
15. 人工使用同一 Rubric，填写评分、理由、最终否决确认、复核状态和最终结论。
16. 检查强制复核和抽样复核完成率及未决否决候选，再按门禁生成初步分析或正式报告。
17. 将代表性失败案例沉淀进 `07_bad_case_log.csv`。

## 数据分工

- `03_benchmark_cases.csv`：稳定用例和规则。
- `benchmark_materials/`：按需承载产品实际输入；不是每个产品必选。
- `benchmark_materials/manifest.json`：素材路径、类型和摘要清单。
- `run.json`：整轮共享元数据。
- `results.csv`：真实候选执行和 Judge 结果。
- `human_review.csv`：人工复核和最终裁决。

## 质量检查

- 用例编号稳定且唯一。
- 评测维度与 Rubric 对齐。
- 一票否决规则与普通评分分离。
- 人工复核触发条件明确。
- Judge 否决候选与人工最终否决分文件记录。
- 同一 case 的每次运行单独占一行；不同产品版本使用独立 run。
- 待执行、失败和空输出不得计入真实评测结果。
- 候选载荷不含预期行为、失败模式、Rubric、优先级、风险或否决规则。
- 素材引用不得越出 Benchmark 目录；素材变化后旧 run 必须拒绝继续。

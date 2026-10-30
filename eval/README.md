# 评测口径

> 项目统一入口：[项目入口](../README.md)。当前质量状态、不同题集来源与重叠关系见 [评测与质量](../docs/QUALITY.md)；以下保留既有操作参考，文中的“当前35题/本轮”指对应历史执行阶段，不代表最终代码已重新通过35题或完成独立质量验收。
>
> `python -m doris_rag evaluate` 是旧诊断入口；`python -m doris_rag.evaluation` 是V2。复杂问题即使不生成答案，也可能产生理解调用。`--run-current`会真实调用服务，离线重评分不调用模型；不要为文档核对重复执行。指标文件中的历史“尚无付费评分/实验环境已删除”不覆盖10月28日独立补充实验记录；后者的辅助评分也不是V2正式独立质量总分。

## V2：离线重评分与语义指标

### 当前35题真实执行（Eval首版）

已取得当前问答模型调用授权后，在独立进程运行：

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --output reports/新的执行目录 --run-current --max-model-calls 40
```

先在`historical/`复用/重评分冻结历史，再在`current/`调用原`pipeline.run_query`。不执行Judge、不生成题、不构建索引；每次模型请求前检查总调用上限，连续三次服务错误熔断，401/403/429直接阻断后续模型请求，不自动重试。被阻断的题仍保留已取得的检索证据和错误。

`current/cases.jsonl`保存逐题答案、指标状态和Trace相对路径；`current/traces/<题目ID>.json`保留实际请求/响应、候选、原片段、补取及实际生成依据；`answers.md`提供可直接阅读的答案与原文链接。`review_queue.jsonl`仅为待审，不自动填审核人或语义标签。

相同输入、生产代码、模型配置和索引的已完成结果可显式复用：

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --output reports/另一个执行目录 --run-current --max-model-calls 40 --reuse-current reports/上一个执行目录/current
```

兼容检查通过且35题均有完整结果时，不加载模型和索引、不调用服务；重新计算评分。生成结果的原run_id与Trace哈希保留，复用的旧用量不计作本轮新增用量。不满足兼容条件则执行新问答，仍受总调用上限约束。输出已存在时拒绝覆盖。

所有“generated”、检索命中和引用编号检查都不是任务完成结论。旧人工/历史复核只属于对应原答案，不会按相同题号自动应用到新答案。

### 已有真实结果重新评分／导入真实审核（不调用模型）

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --score-current reports/上一个执行目录/current --output reports/新的评分目录
```

重新评分始终核对源题集、规则和Trace哈希，使用源运行固化的规则，不静默替换规则。Trace仍指向原运行，无须搬动目录。输出提供完整分母、覆盖率及`answers.md`，但不继承不属于当前答案的历史语义结论。

真实审核人可以复制`review_queue.jsonl`到新文件，只提交已完成审核的题目：保持`id/sample/subject_sha256`不变，填写真实`reviewer`，并在`labels`中提供唯一ID的断言/任务标签。未知用null，不能为补齐覆盖率默认填true。需要相关性AP时另填`context_labels`，按该题`context_ids`逐项提供bool。Codex本轮没有填写这些真实标签。

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --score-current reports/上一个执行目录/current --annotations reports/真实审核.jsonl --output reports/审核后评分
```

审核绑定不一致返回无效数据；不覆盖原问题、答案、Trace。新评分保留提供的审核文件，之后重评分可以继承该审核。完整正确性和完整性标签可计算标准F1，任务要求存在明确失败时独立保留，不被F1等高分覆盖。

同题集、同规则、同评分代码下对比已评分版本：

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --score-current reports/另一个真实运行/current --output reports/对比评分 --compare-current reports/已有评分
```

比较要求题目集合及每项已评题集合一致；缺评分不填整体差值。输出同时保留双方生产代码、索引和模型配置，不能把不同运行条件隐藏为单一提升分数。

当前版本化规则、六维指标公式、所需数据及未评估状态见 [METRICS_V2.md](METRICS_V2.md)。原始题集与下文旧口径保留，不能把规则调整当成产品质量提升。

对已有冻结运行离线计分，不加载索引或调用模型：

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --output reports/新的评分目录
```

结果目录已存在则拒绝覆盖；缺日志、标注、依赖或授权的指标保持null。下文原有`python -m doris_rag evaluate`属于真实执行入口，复杂题即使不加`--answers`也可能调用查询理解模型，不用于本轮离线规则验证。

可选Ragas适配默认关闭；当前实验虚拟环境已删除，须恢复锁定依赖并取得付费授权后才能开展真实Judge评分。本轮不自动安装、不生成新题、不运行付费评测。

## 保留的V1口径

`cases.jsonl` 在首次检索实验前固定，当前 30 道诊断题：29 道有黄金证据，1 道无答案题。跨版本比较题含多个必要证据。每条题目记录来源目录版本、源相对路径、证据词句与答案要点。

先用 `python -m doris_rag evaluate --mode dense` 记录基线，再依据失败类型决定是否优化召回或排序。不能为了提高分数，在观察结果后放宽黄金证据规则。修改题目时必须保留原报告，报告中的用例哈希可区分不同题集。

## 自动计算

- `any_target_hit_rate_at_5`：至少一个必要证据进入前五。
- `all_targets_hit_rate_at_5`：全部必要证据进入前五。
- `mrr_at_5`：第一个核心证据的倒数排名，未命中为 0。
- `not_gold_matched_rate`：未满足黄金规则的片段比例。它只是噪音代理值，可能包含其他有效证据。
- `target_ranks`：逐条必要证据的实际排名，便于区分漏召回、排序靠后和多证据不全。

无答案题不进入召回指标的分母。版本过滤正确不代表知识适用版本已正确判定。

## 人工复核

对报告中的每条证据按旧项目的方法标注：

1. **核心**：当前版本条件下，能直接支撑问题结论。
2. **辅助**：有帮助，但不能独立支撑结论。
3. **噪音**：与问题无关，或用于该问题时版本/模式不适用。

有 LLM 服务后使用 `--answers` 获取实际回答，再填写报告中的 `answer_correct`、`groundedness`、`hallucination`。这些字段默认 `null`，不以关键词出现或引用编号合法自动代替语义判定。

建议同时记录：关键点是否完整、引用是否支持结论、补丁版本和部署模式是否正确、冲突是否明确说明、应拒答时是否拒答。未配置 LLM 的 `llm_not_configured` 是环境未就绪，不能计作正确拒答。

失败归因沿用 Data / Recall / Rank / Generation：先看原文是否覆盖，再看切分是否保留、候选是否包含、排序是否进入上下文，最后判断生成是否遵循证据。重排无法修复缺失数据或未召回证据。

当前题集是开发诊断集，不能代替独立留出集、真实用户测试或 Doris 数据库行为验证。

另有在向量基线执行前固定的 5 道 `holdout_cases.jsonl`，覆盖目录适用范围、CCR 目录缺失误判、默认行为边界表述、补丁版本和 DOC/Sparse 互斥。主键模型题的旧答案要点写“边界不一致”，人工复核发现它不必然构成逻辑矛盾：1.2 引入能力不等于 1.2 默认启用。下一版答案评测应修正该要点；本次冻结的路径/事实检索规则与报告保持不变。默认检索策略只按诊断集比较选择，再单独执行：

```powershell
python -m doris_rag evaluate --cases eval/holdout_cases.jsonl --mode hybrid_precise --answers
```

它仍是很小的人工留出检查，不能据此声称系统已具有普遍的冲突判断能力。

历史验收机器版中的 `reuse.file` 字段保留原脚本来源记录；对应历史专项脚本已移出发布包，这些字段不是当前可执行入口。

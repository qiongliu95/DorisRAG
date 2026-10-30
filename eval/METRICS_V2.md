# DorisRAG 评测口径 V2

版本：`doris-eval-v2-20261025`。本文件定义指标及缺失状态，不改变产品行为或原始题集。

## 六个维度

| 维度/指标 | 计算口径 | 必要数据 | 当前实现及缺失处理 |
|---|---|---|---|
| 检索：必要知识组覆盖@K | 命中组数/必要组数。组间AND，组内等价方案OR；一个方案可要求多个来源同时出现。 | 版本、路径、必要词句及审核后的等价关系；明确有序阶段与K | `score_groups`。词句不全不能只靠路径判事实命中。没有金标准为不适用，缺阶段日志为未评估。 |
| 检索：MRR@K | 首个必要组命中位置倒数；未命中0 | 同上 | `reciprocal_rank`。使用实际顺序位置；不是引用编号的倒数。 |
| 检索：原始候选与送模覆盖 | 原始候选每个子查询/版本分别计算；送模按实际请求证据块检查 | `_rank`追踪、查询/版本、实际生成请求 | 不把各列表第1位混成一个全局第1位。卡片只给页面依据时仅报告`source_coverage`，不冒充事实Recall。 |
| 检索：Context Precision | 完整相关性标签下的AP：Σ(P@i×relevant_i)/检索结果中相关项数；全无关为0 | 特定阶段每个上下文的有序ID与相关标签 | `context_precision`。另列P@K和noise_rate。不是“非gold匹配率”；标签缺失返回null。 |
| 正确性：Factual Precision | 正确的答案断言数/答案断言总数 | 人审断言标签，或已审核参考答案与获准Judge | `semantic_from_labels`；可选Ragas断言/NLI接入。未知标签不以已审子集冒充全量分。 |
| 正确性：Completeness | 已覆盖参考必要断言数/参考必要断言总数 | 充分且版本/条件已审核的参考要点 | 明确使用参考断言作分母；不直接使用保留源码中原生FactualCorrectness的recall分支。 |
| 正确性：F1 | 2PR/(P+R)，两者均0时为0 | 上述P/R均有效 | 记录为本项目标准断言P/R算法，与原生Ragas F1不混名比较。空断言、缺项、越界不计分。 |
| 可信度：Faithfulness | 送模证据支持的答案断言数/答案断言总数 | 完整实际送模上下文、答案、断言蕴含审核/获准Judge | 不用最终引用子集或原始大候选池代替实际上下文。空断言/NaN不能判1。 |
| 可信度：引用结构与语义 | 编号是否在实际送模输入存在；引用是否支持对应断言分别报告 | 实际请求、引用、源SHA；语义需断言到引用的审核标签 | 编号有效只代表结构有效。`citation_entailment`需独立标签，不能由结构分推导。 |
| 解决问题：Task Completion | 单题所有必要任务要求均满足才为1；至少一项必要要求明确失败为0 | 已审核任务判据及完成标签/获准AspectCritic | 语义Judge仅辅助。已确认关键错误独立保留，不能被其他高分覆盖。缺参考或漏步骤不靠`generated`判通过。 |
| 解决问题：Answer Relevancy | 回答反向生成问题与原问题Embedding余弦相似度，乘非含糊回答标记 | 明确可回答题、获准Judge、显式注入评测Embedding | 可选Ragas `ResponseRelevancy(strictness=1)`，该参数与默认3不同须记录。正确澄清/拒答不按普通回答相关性扣分。 |
| 边界：澄清/拒答/冲突/版本适用 | 对应审核行为要求逐项判；版本适用看正文/补丁/模式条件 | expected_behavior、允许的未知项、预期范围、必要条件及标签 | `boundary_handling`、`version_applicability`。`source_version`只证明出处，不能证明功能适用。状态字符串不自动给分。 |
| 边界：会话稳定性 | 在已标注的主题继承、范围修改、换题等转移上判定 | session_id、message_id、原问题、实际管线问题、前后状态与预期转移 | 复用既有B/换题审核；保留实际管线问题给语义Judge，避免仅拿“所有版本都查一下”独立打分。没有转移标签不报整体稳定率。 |
| 性能：延迟/成功率/资源 | 同批次阶段耗时分布；已记录模型HTTP成功率与任务成功率分开；资源取实测峰值 | 阶段计时、全部预期请求及终态、并发/冷暖条件、资源采样 | 本轮仅能计算已有串行记录。未观察请求不当成功，HTTP200不代表任务完成，无资源采样不报峰值。 |

必要组覆盖是非穷尽gold规则分；在标注未穷尽时不宣称真实语义Recall。等价文档必须满足同一事实的版本及条件，不能全局按相似度放宽。

## 规则与评分状态

- `scoring_rules_v2.json`固定原题、案例卡、冲突补充判据和新增等价来源SHA。相同ID换了正文会拒绝套用旧规则。审核来源变更即停止重放。
- `index_v1`允许4.x FE配置页作为完整事实的等价来源；`stream_newkey`允许3.x列更新页。不是允许任意同主题页面。
- `retention`的旧gold后缀`retention_count`显式修订为原问题/原文中的完整参数`partition.retention_count`，同时检查互斥条件；不全局放宽标识符边界。
- `rl_be3`、`compare_rl`必须保留3.x两个冲突出处；A02/A03的任务判据也覆盖旧卡“唯一默认值5”的要求。事实争议保持未裁定。
- `evaluated`：有足够数据计算，数值0可能是真实观察失败。
- `not_evaluated`：缺日志、审核、依赖、授权或Judge；数值null。
- `partially_evaluated`：部分标签/题目未完成；总体数值null，已评子集均值单列并显示覆盖率。
- `not_applicable`：如无答案题不进入检索召回分母。
- `invalid_data` / `invalid_judge_output` / `judge_error`：标注不完整、返回NaN/越界、解析或调用失败；不转成0或通过。
- 已确认一项必要任务失败时，可以确定Task Completion=0，即使其他要求尚未审核；未审核项仍保留。
- 不提供六维综合分数；整体验收仍为`not_determined`，已有关键失败则`fail`。

## 旧口径与阶段日志

`matches`及旧批量入口的计算保留为`legacy_v1`；历史文件不改写。新入口为离线重放，旧批量入口新增`scoring_v2.json`侧文件；生成后仅有已引用依据时，不计算原始召回。

分别记录：原始子查询/版本候选列表 → 最终选中的原片段 → 补取后证据 → 实际模型输入 → 实际引用依据。历史记录缺哪一步，就不能声称已经衡量该步。候选排名、页面覆盖、事实覆盖、答案支撑是四种不同观察。

模型输入只从真实请求中的固定来源头解析；格式不完整或来源头不一致时不截取一个子集伪装完整上下文。语义输入保留原用户问题与实际管线问题，避免追问失去会话语义。

## 标注与Judge输入

确定性标签接口 `semantic_from_labels` 接受带唯一ID的列表：

- `answer_claims`：`correct`、`supported`；
- `reference_points`：`covered`；
- `task_requirements`、`boundary_items`、`version_items`：`passed`；
- `citation_claims`：`supported`；`relevance_items`：`satisfied`。

每个标签为true/false/null。复核材料须绑定题目、答案、上下文及规则版本；低层计算函数不保证人工真值质量。冻结历史复核通过原始文件SHA绑定，不把旧`correctness=pass_within_case`折算成断言正确率。

正式使用新增标注时调用`score_reviewed_labels(sample, annotation, rules_sha256)`：`annotation`须含非空`reviewer`、由`review_subject`计算的`subject_sha256`及`labels`。绑定完整样本与规则，答案、上下文或规则改变时拒绝串用旧标签。不能只调用低层标签均值函数就宣称审核有效。

`score_semantics`输入包括 `user_input`（实际管线问题）、`response`、`retrieved_contexts`、`contexts_complete`。参考评分还要求`reference`、`reference_reviewed=true`、`reference_applicability_reviewed=true`、`reference_versions`与`source_versions`一致。审核涵盖正文条件和冲突，不只核对目录版本。现有`expected_keypoints`和自动生成参考均不自动满足这个审核条件。

Task Judge需要`task_rubric`；Answer Relevancy还需要`expected_behavior=answer`和显式Embedding。已确认关键失败通过`known_critical_failures`保留，Judge不能解除。

## 安全执行与复现

离线命令，无模型和索引加载：

```powershell
python -B -m doris_rag.evaluation reports/quality_baseline_20261025 --output reports/新的评分目录
```

输出已存在时拒绝覆盖。清单固定全部输入、规则、计分代码及适配器SHA；正文不插入当前时间，便于内容哈希复现。`compare_scores`只对同题集、规则与计分代码进行同口径比较；分母或评分缺失不计算整体提升。不同运行批次及模型/配置须分开报告，不混算通过率。

语义适配默认 `allow_model=False`，不会读取模型密钥或调用模型。`configured_judge`还要求显式付费授权记录、已报告费用标记、正整数调用上限及新追踪文件。本轮未使用该入口。它仅在独立评测进程复用现有`generation.chat_completion`，记录真实请求/响应、模型、参数、用量和错误，不改变产品提示。调用次数上限不等于美元上限，后续必须另行估算和授权。

目前实验虚拟环境已被此前清理删除，系统Python无Ragas。源码及锁定依赖仍保留；本轮不自动恢复107项依赖。原生适配测试在缺依赖时明确跳过，不能据此宣称Ragas集成已实跑通过。恢复环境时仅恢复依赖并运行`test_evaluation_ragas.py`；不要运行旧生成脚本来“验证评分”，它们会调用模型并可能覆盖历史题集。

按保留源码估算，断言P/R/F1联合需4次Judge调用，Faithfulness通常2次，Task约1次，Context Precision每条上下文1次，Relevancy此配置1次。格式修复可能增加调用，均受总调用上限约束。当前没有付费评分结果，也没有Judge与人工的一致性校准结果。

保留源码的Answer Relevancy直接使用余弦相似度，原始值可能落在[-1,1]；适配器保留原值和范围，不把负余弦误当非法输出或静默截为0。其他上述比例指标范围为[0,1]；NaN/无穷仍为无效结果。

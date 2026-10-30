# 资料来源与证据索引

本索引按用途组织，不把所有开发产物复制成新文档。相对路径在项目内可复用；历史文件中的绝对路径或外部依赖可能只适用于原机器。没有重新访问外部平台或开展需求研究。当前核对日期2026-10-29。

## 证据优先级与身份

判断“实现什么”先查当前源代码/配置；判断“验证了什么”查对应实际输入、messages、答案、判定及manifest；判断“要求什么”查原基线、用户后续确认的产品契约与案例卡。历史总结用于定位，不能覆盖原记录；当前代码存在也不能推翻旧失败。

整理开始的71文件身份检查（本地归档：../reports/documentation_20261029/current_identity_check.json），参照 最新freeze（本地归档：../reports/serious_defects_regression_20261028/final_freeze.json）及 真实运行manifest（本地归档：../reports/serious_defects_regression_20261028/live/run_manifest.json）。本次修改前清单 baseline.json（本地归档：../reports/documentation_20261029/baseline.json）；最终只有入口README偏离该71文件冻结，其余70个相同，保护验证与链接检查见 整理报告（本地归档：../reports/DOCUMENTATION_RECONCILIATION_20261029.md）。项目没有Git仓库，不提供虚构commit。上述哈希不是驻留进程身份检查。

## 原始需求与规划

| 来源 | 性质与适用性 |
| --- | --- |
| [V1技术设计基线](reference/TECHNICAL_BASELINE_20261023.md) | 10月23日原件；数据/知识/架构目标，默认版本等须结合后续产品确认 |
| [前端交互与Session基线](reference/UI_SESSION_BASELINE.md) | 原件；后续已实现与未验收须以当前代码/结果区分 |
| [整体收尾规划](reference/CLOSEOUT_PLAN_20261024.md) | 原件；正文中的历史“未授权”已由10月24日明确实施授权覆盖，不是新的权限限制 |
| 产品行为契约（本地归档：../reports/V1_PRODUCT_CONTRACT_20261024.md） | 确认默认4.x、会话、状态、性能观察和架构边界；后续调用上限差异须显式标注 |
| [22卡阅读版](../eval/V1_FINAL_ACCEPTANCE_20261024.md) / [机器版](../eval/v1_final_acceptance_20261024.json) | 原始验收要求，不是已通过清单 |
| [A02/A03冲突补充判据](../eval/V1_ACCEPTANCE_CLARIFICATIONS_20261024.md) | 保留5/1024官方冲突，区分处理正确性与事实裁定，不改原卡 |
| [项目AGENTS](../AGENTS.md) | 最小修改、完整离线回归、CONV006顺序及真实验收约束 |

三份原件从本机下载目录 `DORISRAG_完整历史资料包_20261029.zip` 中按原始字节恢复，没有编辑。来源成员及SHA如下；机器可读记录见 restored_sources.json（本地归档：../reports/documentation_20261029/restored_sources.json）。

| 文件 | ZIP成员（共同前缀 `DORISRAG_history_20261029/originals/`） | SHA256 |
| --- | --- | --- |
| TECHNICAL_BASELINE_20261023.md | S020.md | `6d60b086a6d1238e3267dcf9ffab2167b0b0163dd86102dff3da53be2f49dd7f` |
| UI_SESSION_BASELINE.md | S022.md | `a0fe7b220b7b9e20a4823af63291f2fa0c78323fecec7e3082ce095fd2160229` |
| CLOSEOUT_PLAN_20261024.md | S023.md | `9ef3b29d0cf8400923bc9097a98d66955aa9de539bfcec2398ac87231e114652` |

同目录的《DORISRAG_历史资料总册_20261029.md》是二次汇编目录，声明未检查Windows工程树；可用于找历史来源，不能证明当前实现。资料包包含旧归档及外部记录，不代表完整聊天历史。本次只归档上述关键原件，不重复导入所有备份、索引或外部平台记录；原包位于用户下载目录，不是项目运行依赖。

## 当前实现入口

| 内容 | 一手来源 |
| --- | --- |
| 依赖、配置、启动 | [config.json](../config.json)、[固定依赖](../requirements-lock.txt)、[宽范围依赖](../requirements.txt)、环境模板（本地归档：../.env.example）、[CLI](../doris_rag/__main__.py)、[启动脚本](../start_v1.cmd) |
| 来源/解析/切片 | [documents](../doris_rag/documents.py)、[markdown](../doris_rag/markdown.py)、[html_tables](../doris_rag/html_tables.py)、[chunking](../doris_rag/chunking.py)、[audit](../doris_rag/audit.py) |
| 模型和索引 | [common](../doris_rag/common.py)、[indexing](../doris_rag/indexing.py)、活动指针（本地归档：../data/round1_20261023/index/current.json）、[构建检查](../reports/round1_20261023/build_checks.json)、[构建状态](../reports/round1_20261023/run_status.json) |
| 任务/检索/证据/生成 | [query_understanding](../doris_rag/query_understanding.py)、[lexical](../doris_rag/lexical.py)、[retrieval](../doris_rag/retrieval.py)、[context](../doris_rag/context.py)、[generation](../doris_rag/generation.py)、[pipeline](../doris_rag/pipeline.py) |
| Session/HTTP/UI | [sessions](../doris_rag/sessions.py)、[web](../doris_rag/web.py)、[presentation](../doris_rag/presentation.py)、[页面](../ui/index.html)、[交互](../ui/app.js) |
| 追踪/运行日志 | [telemetry](../doris_rag/telemetry.py)、[runtime_logging](../doris_rag/runtime_logging.py) |
| 评测 | [evaluation](../doris_rag/evaluation.py)、[evaluation_semantics](../doris_rag/evaluation_semantics.py)、[评测操作](../eval/README.md)、[指标定义](../eval/METRICS_V2.md)、[规则](../eval/scoring_rules_v2.json) |

不要在公开文档索引链接.env或用户会话数据库。检查会话状态需遵循 [运维](OPERATIONS.md) 的数据保护与只读限制。

## 研究、题集与原始材料

- 社区覆盖研究（本地归档：../experiments/coverage_audit_20261023/REPORT.md）：题源、官方证据和“能回答什么”的边界；历史小语料身份须保留。
- 早期Ragas研究（本地归档：../experiments/ragas_20261023/REPORT.md）：源码快照、题目生成、失败路径及实验Embedding；不能把生成参考当Gold真值。
- [固定30题](../eval/cases.jsonl)、[holdout5题](../eval/holdout_cases.jsonl)、[第二轮10题](../eval/round2_cases.jsonl)、35题冻结目录（本地归档：../reports/quality_baseline_20261025/）：相互重叠与来源关系见QUALITY。
- 原输入来源审查（本地归档：../input_materials/SOURCE_REVIEW.md）、原始输入目录（本地归档：../input_materials/inputs/）、来源记录目录（本地归档：../input_materials/sources/）：A12 #57652、A13 #34168、A16 #47962原作者材料与改写差异，不混入维护者建议。
- 100题有效材料与结果（本地归档：../experiments/final_acceptance_20261028/FINAL_REPORT.md）、有效题（本地归档：../experiments/final_acceptance_20261028/valid_cases.json）、指标（本地归档：../experiments/final_acceptance_20261028/metrics.json）、缺陷（本地归档：../experiments/final_acceptance_20261028/DEFECTS.md）：包含生成、剔除、纠错、中间运行和辅助评审；正式100题与249次过程尝试分开。
- 框架研究（本地归档：../reports/FRAMEWORK_COMPARISON_20261028.md）：仅研究机制，未迁移框架；本次没有重新联网选型。

## 开发、验证与问题闭环

| 阶段 | 主记录 | 阅读时必须保留的限制 |
| --- | --- | --- |
| 基线差距/两轮实施 | 差距D01–D24（本地归档：../reports/BASELINE_CONFORMANCE_REVIEW_20261023.md）、第一轮（本地归档：../reports/ROUND1_KNOWLEDGE_INDEX_REPORT_20261023.md）、第二轮（本地归档：../reports/ROUND2_QUERY_APPLICATION_REPORT_20261023.md） | 当时范围与构建状态，不当最新全系统验收 |
| 早期验收/稳定性 | 10月24日系统（本地归档：../reports/V1_SYSTEM_ACCEPTANCE_20261024.md）、收尾（本地归档：../reports/V1_CLOSEOUT_20261024.md）、10月25日稳定性（本地归档：../reports/V1_STABILITY_CLOSEOUT_20261025.md）、基础清单（本地归档：../reports/V1_BASIC_FUNCTION_ACCEPTANCE_20261025.md） | 旧首页曾把这些当最新；后续多次改造/运行已取代其“当前状态” |
| 原题专项/原30题 | 核心验收（本地归档：../reports/v1_core_acceptance_20261026/REPORT.md）、停止记录（本地归档：../reports/v1_last_directed_fix_20261026/REPORT.md）、30题真实（本地归档：../reports/original30_live_20261026/REPORT.md） | 不择优保留一次成功；93.33%仅当批辅助评审 |
| V2执行与分母 | 首次执行（本地归档：../reports/eval_first_run_20261025/REPORT.md）、分母修复（本地归档：../reports/denominator_fix_20261025/REPORT.md）、评分汇总（本地归档：../reports/denominator_fix_20261025/scored/summary.json） | 旧“评分入口仍阻塞”已过时；局部审核仍不能得到整体质量分 |
| 架构读图/任务契约 | 早晨架构审查（本地归档：../reports/ARCHITECTURE.md）、第一批实现（本地归档：../reports/architecture_batch1_20261028/REPORT.md）、生成契约（本地归档：../reports/generation_task_contract_20261028/REPORT.md） | ARCHITECTURE是改造前只读快照，不是现行技术设计；字段拆分不能单独证明关系识别正确 |
| 关系识别 | 只读审计（本地归档：../reports/context_relation_audit_20261028/REPORT.md）、有限授权修复（本地归档：../reports/session_relation_exception_20261028/REPORT.md） | 保留历史三轮与例外；13次请求、B3 PARTIAL，不等于全部多轮通过 |
| Trace/UX | 增量记录（本地归档：../reports/trace_persistence_20261027/REPORT.md）、界面（本地归档：../reports/v1_ux_20261028/REPORT.md） | 默认轻量覆盖旧“默认完整Trace”；夹具UI检查不等于真实模型回答正确 |
| 完整22卡/日切日志 | 报告（本地归档：../reports/final_acceptance_20261028/REPORT.md）、逐卡判定（本地归档：../reports/final_acceptance_20261028/case_results.md）、原始评审JSON（本地归档：../reports/final_acceptance_20261028/acceptance_review.json） | 9/5/8属于后续专项前；正式用户服务未重启；失败回滚尝试已撤销 |
| 最新D02/D03/D05和CONV006 | 报告（本地归档：../reports/serious_defects_regression_20261028/REPORT.md）、实际逐题（本地归档：../reports/serious_defects_regression_20261028/actual_inputs_answers.md）、实际JSON（本地归档：../reports/serious_defects_regression_20261028/actual_acceptance.json）、修复账本（本地归档：../reports/serious_defects_regression_20261028/repair_ledger.json） | 提取/三题证据修复通过，D03 FAIL，生成残余未关；不发布新全局分 |
| 最新离线/反事实 | 302项记录（本地归档：../reports/serious_defects_regression_20261028/full_regression.json）、固定池摘要（本地归档：../reports/serious_defects_regression_20261028/frozen_pool_summary.json） | 7SKIP；34可回放/4未ready，不能当新35题问答验收 |

最新真实诊断入口：native_traces目录（本地归档：../reports/serious_defects_regression_20261028/live/native_traces/）、证据路由（本地归档：../reports/serious_defects_regression_20261028/D05_evidence_routes.json）、官方冲突条件（本地归档：../reports/serious_defects_regression_20261028/D03_official_scope_evidence.json）。逐题JSON包含message/Session/Trace路径，以此定位，不凭报告摘句猜测实际messages。

## 资料缺口及维护

- 没有完整原始聊天导出、统一需求变更签署表或全部外部记录的独立复核；项目起始日期不能仅按最早可见10月23日推定。
- 没有同最终代码身份的全部22卡/35题/100题重跑与完整独立审核；不为填齐文档而补测、造分或降低判据。
- 没有统一历史费用账本、固定不可变生成模型版本、最新驻留进程身份和全批次性能SLA；缺失计量不补估成实际值。
- 主运行lock与可选开发/实验环境分离，缺新机器完整安装/迁移/备份恢复验收；历史报告中某些已清理环境或绝对路径无法直接复现。
- 原始官方站点目录是三个版本系列快照，缺所有补丁独立来源；遇到文档自身冲突只能记录，不推定真实产品事实。

以后追加结果保留旧文件、输入与身份；更新本索引的适用范围及主题文档，不把历史过程再次复制成长篇“最新总结”。

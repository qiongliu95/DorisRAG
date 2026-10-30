# DorisRAG V1 最终验收案例

22 张案例卡，复用既有诊断/留出/第二轮用例、已核查社区场景及 Ragas 候选；本轮未调用模型、未重新出题或联网研究。
本文由 `eval/v1_final_acceptance_20261024.json` 导出。单题/变体每次新建会话；只有标记“连续对话”的案例保留状态。
知识问答最终验收仍须新 768 维索引的真实 HTTP/UI/LLM/Dense 结果，当前引用核验不等于答案通过。

共同判据：默认仅4.x；本轮明确版本优先；不自动扩大范围；产品事实由允许范围官方文档支持；用户事实/规则/推论/未知分开。
输入材料的历史范围字段不能覆盖上述新契约。无数值 SLA；记录客户端反馈、完整耗时、调用次数及冷/热状态。
列出的官方依据是已核对锚点；替代文档须按同样版本/原文条件人工核实，不改写旧诊断集评分。澄清前未检索与范围错误分别判断。

## A01 · 短函数与最小示例

方式：单轮。预期来源范围：['4.x']。

输入：

1. 输入

````text
PI() 返回什么数值？
````


预期与判定：

- 未给版本时只取4.x；无固定版本澄清表单
- 给出Pi及文档数值3.141592653589793，示例和引用可追溯
- 简单链路不额外做LLM拆解

知识/行为依据：

- [4.x · pi.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/sql-manual/sql-functions/scalar-functions/numeric-functions/pi.md)
- 复用：`eval/cases.jsonl` → `pi4`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A02 · 单版本参数默认值

方式：单轮。预期来源范围：['3.x']。

输入：

1. 输入

````text
Doris 3.x 的 max_routine_load_task_num_per_be 默认值是多少？
````


预期与判定：

- 只检索3.x，不夹带2.1/4.x
- 默认值5和适用条件来自对应FE章节

知识/行为依据：

- [3.x · fe-config.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/admin-manual/config/fe-config.md)
- 复用：`eval/round2_cases.jsonl` → `single_version`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A03 · 明确跨版本比较

方式：单轮。预期来源范围：['2.1', '3.x']。

输入：

1. 输入

````text
比较 Doris 2.1.4 和 3.x 的 max_routine_load_task_num_per_be 默认值。
````


预期与判定：

- 分别引用2.1与3.x，不混写成全局默认值
- 2.1.4起1024与3.x的5分别有证据；不覆盖所有补丁

知识/行为依据：

- [2.1 · fe-config.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/admin-manual/config/fe-config.md)
- [3.x · fe-config.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/admin-manual/config/fe-config.md)
- 复用：`eval/round2_cases.jsonl` → `comparison`
- 复用：`eval/cases.jsonl` → `compare_rl`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A04 · 日志与保留旧示例的补丁边界

方式：独立变体（逐个新建会话）。预期来源范围：['4.x']。

输入：

1. 输入

````text
Doris 4.1.4 升级后 Upload Action 请求失败。日志如下：
POST /api/default_cluster/example_db/example_tbl/upload HTTP/1.1
HTTP/1.1 404 Not Found
为什么失败？还有哪些条件需要确认？
````

2. 输入

````text
Doris 4.1.4 升级后 Upload Action 失败，以下为日志：
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
2026-10-23 10:00:00 INFO client retry pending
POST /api/default_cluster/example_db/example_tbl/upload HTTP/1.1
HTTP/1.1 404 Not Found
文档如何解释这个现象？还有哪些信息需要确认？
````


预期与判定：

- 保留4.1.4移除规则，旧POST示例不覆盖警告
- 用户404与文档规则只能形成有前提推论，不能认定集群根因
- 缺少的信息明确指出，不重复追问已给版本
- 长日志提取为少量短知识子问题，事实可回原句；不能静默截断整份材料作Dense输入

知识/行为依据：

- [4.x · upload-action.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/admin-manual/open-api/fe-http/upload-action.md)
- 复用：`eval/round2_cases.jsonl` → `scenario_upload`
- 复用：`eval/holdout_cases.jsonl` → `upload_patch_boundary`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A05 · 目录版本不等于适用性

方式：单轮。预期来源范围：['2.1']。

输入：

1. 输入

````text
我的 Doris 2.1 集群读取 Hive 子目录数据失败。配置如下：
hive.recursive_directories=true
为什么没生效？能否证明是集群故障？
````


预期与判定：

- 只取2.1来源，保留正文自3.0.2支持条件
- 不因目录认定2.1支持，也不自动改查3.x
- 不能据材料证明集群故障

知识/行为依据：

- [2.1 · hive-catalog.mdx](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/lakehouse/catalogs/hive-catalog.mdx)
- 复用：`eval/round2_cases.jsonl` → `scenario_config`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A06 · 组合配置与关联说明

方式：独立变体（逐个新建会话）。预期来源范围：['4.x']。

输入：

1. 输入

````text
我们在 Doris 4.x 测试 VARIANT 配置时失败。配置如下：
DOC mode=true
Sparse=true
文档对它们有什么约束？请区分已知条件和推论。
````

2. 输入

````text
我的 Doris 4.x 执行 SQL 失败，SQL 如下：
```sql
CREATE TABLE test_variant (id INT, v VARIANT) PROPERTIES ('variant_enable_doc_mode'='true', 'variant_max_subcolumns_count'='2048');
```
文档怎样约束这些模式和参数？这些材料足以确认根因吗？
````


预期与判定：

- 解释DOC mode与Sparse互斥，保留条件与原文
- 区分用户配置、规则与推论；不能声称执行过SQL
- 若改用已有scenario_sql变体，推荐storage_format=V3不能冒充强制要求，未有证据的表级配置支持应说明未知

知识/行为依据：

- [4.x · variant-workload-guide.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/sql-manual/basic-element/sql-data-types/semi-structured/variant-workload-guide.md)
- 复用：`eval/round2_cases.jsonl` → `scenario_two_constraints`
- 复用：`eval/round2_cases.jsonl` → `scenario_sql`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A07 · 新增变量及Scanner例外

方式：单轮。预期来源范围：['4.x']。

输入：

1. 输入

````text
Doris 4.1.4 中 enable_expr_zonemap_filter 控制什么？File Scanner V2 受它影响吗？
````


预期与判定：

- 结合正文核对Boolean、false及4.1.4新增条件
- 保留File Scanner V2例外，不把条件丢掉

知识/行为依据：

- [4.x · session-variables.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/sql-manual/basic-element/session-variables.md)
- 复用：`experiments/ragas_20261023/final_run/questions.json` → `Q05`

Ragas题目经人工收窄，参考答案不作为真值；最终需Dense查证。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A08 · 参数零值和负值边界

方式：单轮。预期来源范围：['4.x']。

输入：

1. 输入

````text
Doris 4.1.4 的 runtime_filter_tree_publish_max_send_bytes 默认值是什么，设0或负数分别怎样？
````


预期与判定：

- 正文核对默认268435456、0关闭树形分发、负数非法
- 保留4.1.4新增条件，不用Ragas参考替代来源

知识/行为依据：

- [4.x · session-variables.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/sql-manual/basic-element/session-variables.md)
- 复用：`experiments/ragas_20261023/final_run/questions.json` → `Q06`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A09 · JDBC与向量Prepared Statement跨文档

方式：单轮。预期来源范围：['4.x']。

输入：

1. 输入

````text
Doris 4.x 通过 MySQL JDBC 连接时，向量查询如何使用 Prepared Statement？哪些相关命令不能直接用 mysql 客户端执行，连接参数怎么写？
````


预期与判定：

- 连接方式和向量Prepared Statement分别有文档依据
- useServerPrepStmts=true、占位符与方法须查正文；不能把mysql客户端的特定命令限制泛化到所有SQL
- 代码或表格补取不完整时，不伪装完整可执行步骤

知识/行为依据：

- [4.x · mysql-proto.mdx](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/connection-integration/mysql-proto.mdx)
- [4.x · overview.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/table-design/index/vector-index/overview.md)
- 复用：`experiments/ragas_20261023/final_run/questions.json` → `Q19`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A10 · 原文性能示例不能承诺收益

方式：单轮。预期来源范围：['2.1', '3.x']。

输入：

1. 输入

````text
比较 Doris 2.1 和3.x 的 NGram BloomFilter 使用条件。文档示例从7.60秒到0.93秒，是否能保证我的查询也快8倍？
````


预期与判定：

- 保留版本及适用条件，重复段不算额外推理证据
- 7.60/0.93只作为文档实验示例，不能保证用户性能
- 不把这组数字计作DorisRAG响应耗时

知识/行为依据：

- [3.x · ngram-bloomfilter-index.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/table-design/index/ngram-bloomfilter-index.md)
- [2.1 · ngram-bloomfilter-index.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/table-design/index/ngram-bloomfilter-index.md)
- 复用：`experiments/ragas_20261023/final_run/questions.json` → `Q14`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A11 · 有相关知识但缺兼容操作步骤

方式：单轮。预期来源范围：['4.x']。

输入：

1. 输入

````text
Apache Doris 4.0 升级后，ANN vecor 搜索和 BM25/SEARCH 全文检索咋兼容配？
````


预期与判定：

- 已证实的功能引入可按证据说明
- 缺少ANN与BM25升级兼容步骤时说明不足，不编造开关或命令
- 不足不自动去3.x/2.1搜答案

知识/行为依据：

- [4.x · overview.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/table-design/index/inverted-index/overview.md)
- [4.x · overview.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/table-design/index/vector-index/overview.md)
- 复用：`experiments/ragas_20261023/final_run/questions.json` → `Q21`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A12 · 社区：部分列更新条件与修复未知

方式：单轮。预期来源范围：['3.x']。

输入：

1. 输入

````text
Doris 3.1，Unique Key MoW 的 Routine Load 配了 partial_columns=true，消息只有 id 和 age，COLUMNS 却声明全部列，其他列变成NULL。文档能解释哪些条件？能确定是Bug或哪版已修复吗？
````


预期与判定：

- 保留缺失字段与显式NULL的区别；检查Key与更新列、列映射
- 不能把4.x后续支持倒推3.1补丁，也不能把PR合并或Stale关闭当已发布修复

知识/行为依据：

- [3.x · partial-column-update.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/data-operate/update/partial-column-update.md):L54–L71；部分列更新必须同时指定列集合和全部Key的操作要求
- [3.x · partial-column-update.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/data-operate/update/partial-column-update.md):L123–L137；固定列更新的批次列集合约束、灵活更新3.1起且该页限定Stream Load
- 复用：`experiments/coverage_audit_20261023/evidence.json` → `C01`

使用2026-10-23已完成的原帖核查和纠偏，输入是整理后的保真场景，不是原帖逐字引文；本轮不重新联网。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A13 · 社区：内存指标与根因边界

方式：单轮。预期来源范围：['2.1']。

输入：

1. 输入

````text
Doris 2.1.1 查询结束后BE内存没降，Memory Tracker与RSS不一致并出现OOM。能直接断定是内存泄漏吗？需要哪些证据？
````


预期与判定：

- 按2.1证据区分Tracker/RSS、缓存与统计口径
- 不能从RSS不降直接认定泄漏，或断言一定不是泄漏；必要运行态材料须列明

知识/行为依据：

- [2.1 · memory-tracker.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/admin-manual/trouble-shooting/memory-management/memory-feature/memory-tracker.md):L17–L25；Tracker子集、虚拟内存与进程物理内存口径差异
- [2.1 · memory-tracker.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/admin-manual/trouble-shooting/memory-management/memory-feature/memory-tracker.md):L49–L57；2.1.5前SegmentCache统计与Heap Profile排查步骤
- [2.1 · jemalloc-memory-analysis.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/admin-manual/trouble-shooting/memory-management/memory-analysis/jemalloc-memory-analysis.md):L35–L45；线程idle时Thread Cache可不随查询结束立即清理
- 复用：`experiments/coverage_audit_20261023/evidence.json` → `C05`

使用2026-10-23已完成的原帖核查和纠偏，输入是整理后的保真场景，不是原帖逐字引文；本轮不重新联网。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A14 · 社区：分区开关和命名的纠偏

方式：单轮。预期来源范围：['3.x']。

输入：

1. 输入

````text
Doris 3.1.4-rc02，Auto Partition与Dynamic Partition联用，dynamic_partition.enable=false。历史分区不清理，改为true后命名又不同，如何按文档解释？
````


预期与判定：

- 必须使用已补正的false前提，解释动态回收与按需创建职责
- 命名差异按两种创建路径说明，不把外部评论/closed当产品证据
- 不能宣称已验证rc02全部实现，不再追问已明确的enable值

知识/行为依据：

- [3.x · auto-partitioning.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/table-design/data-partitioning/auto-partitioning.md):L217–L233；联用职责、命名不确定性以及支持版本
- [3.x · dynamic-partitioning.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-3.x/table-design/data-partitioning/dynamic-partitioning.md):L125–L142；enable开启开关、start回收规则；false禁用动态分区职责
- 复用：`experiments/coverage_audit_20261023/evidence.json` → `C16`

使用2026-10-23已完成的原帖核查和纠偏，输入是整理后的保真场景，不是原帖逐字引文；本轮不重新联网。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A15 · 社区：未收录历史版本

方式：单轮。预期来源范围：[]。

输入：

1. 输入

````text
Doris 2.0.2 执行 date_trunc(...,'all') 报错，历史文档却说非法单位返回NULL，哪个才对？
````


预期与判定：

- 明确2.0.2不在2.1/3.x/4.x收录范围内，停止越界检索或请求选择已有版本
- 不得自动改查2.1/4.x，更不能引用1.2/2.0历史页裁定2.0.2实现或修复状态

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`experiments/coverage_audit_20261023/evidence.json` → `C19`

使用2026-10-23已完成的原帖核查和纠偏，输入是整理后的保真场景，不是原帖逐字引文；本轮不重新联网。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A16 · 社区：INT数值边界而非名称

方式：单轮。预期来源范围：['2.1']。

输入：

1. 输入

````text
Doris 2.1.6 动态分区表中INT月值为202503，分区名仍是p202502，但实际边界出现20250201这样的YYYYMMDD数值，写入失败。如何核对？能否确定是命名问题或Bug？
````


预期与判定：

- 不再使用错误的“分区名称增加日成分”摘要
- 解释Range左闭右开及数值边界核对；INT与文档DATE/DATETIME限制的适用缺口必须保留
- 不能仅据滚动文档裁定2.1.6实现Bug或认为改名能修好

知识/行为依据：

- [2.1 · dynamic-partitioning.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/table-design/data-partitioning/dynamic-partitioning.md):L23–L31；本地2.1动态分区限制为DATE/DATETIME，与INT案例存在适用缺口
- [2.1 · manual-partitioning.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-2.1/table-design/data-partitioning/manual-partitioning.md):L21–L42；Range左闭右开，名称与边界独立；用于核对YYYYMM值是否落入真实数值边界
- 复用：`experiments/coverage_audit_20261023/evidence.json` → `C20`

使用2026-10-23已完成的原帖核查和纠偏，输入是整理后的保真场景，不是原帖逐字引文；本轮不重新联网。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A17 · 必要澄清与短句接续

方式：连续对话。预期来源范围：['4.x']。

输入：

1. 输入

````text
帮我看看
````

2. 输入

````text
我想知道 PI() 返回什么？
````


预期与判定：

- 目标缺失只问一项关键材料，不能强制先填版本
- 短句回复后继续当前会话，简单问题直接回答；事实只来自用户原话

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- [4.x · pi.md](E:/AI-Research/DorisRAG/doris-website-master/doris-website-master/i18n/zh-CN/docusaurus-plugin-content-docs/version-4.x/sql-manual/sql-functions/scalar-functions/numeric-functions/pi.md)
- 复用：`tests/check_v1_application.py` → `validate:clarify`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A18 · 补充、纠正澄清、临时比较与换话题

方式：连续对话。预期来源范围：见各步骤/判据。

输入：

1. 输入；范围 3.x

````text
Doris 3.x，我的配置 enable_insert_strict=false，为什么导入失败？
````

2. 输入；范围 3.x

````text
补充：当前还没有提供完整错误日志。
````

3. 输入

````text
纠正一下，刚才说错了
````

4. 输入；范围 3.x

````text
enable_insert_strict=true
````

5. 输入；范围 2.1

````text
纠正一下，我用的是 Doris 2.1.4
````

6. 输入；范围 3.x,4.x

````text
那比较3.x和4.x这个参数的规则
````

7. 输入；范围 2.1

````text
那具体含义呢？
````

8. 输入；范围 4.x

````text
换个问题，PI()返回什么？
````


预期与判定：

- 补充保留目标，纠正澄清后新值替代旧false；不是把false与true并列
- 版本纠正清旧证据；临时比较后仍回到2.1目标，不能带上比较任务
- 新话题清除旧配置/版本；不能把上一回答的推论记作用户事实

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`tests/check_v1_flow_review.py` → `real_http_state_steps`
- 复用：`tests/test_sessions.py` → `ConversationRules`

第三轮是澄清而非检索；字段实际语义与用户集群根因仍需官方证据，不能只靠状态断言答案正确。

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A19 · 版本矩阵与不足不扩大范围

方式：独立变体（逐个新建会话）。预期来源范围：见各步骤/判据。

输入：

1. 输入；范围 4.x

````text
PI() 返回什么？
````

2. 输入；范围 3.x

````text
Doris 3.x 的 PI() 返回什么？
````

3. 输入；范围 3.x,4.x

````text
比较 Doris 3.x 和4.x 的 PI()
````

4. 输入；范围 4.x,3.x,2.1

````text
请查所有版本，PI() 返回什么？
````

5. 输入；范围 4.x

````text
Doris 4.x 的 quantum_auto_fix_mode 默认值和开启方法是什么？
````

6. 输入；范围 4.x

````text
quantum_auto_fix_mode 默认值和开启方法是什么？
````


预期与判定：

- 前三种不夹带允许范围以外的证据；只有明确全版本才覆盖全部
- 缺参数证据时不编造默认值/命令，不断言不存在，不扩大范围
- 规则层可离线验证，真实证据集合和回答须新索引完成后复验

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`tests/check_v1_application.py` → `validate:all/unknown`
- 复用：`tests/test_sessions.py` → `test_latest_default_and_explicit_only`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A20 · 首页与会话完整生命周期

方式：页面操作/受控回放。预期来源范围：见各步骤/判据。

输入：

1. 输入

````text
三个场景逐个点击→修改示例→新建/切换会话→刷新→取消删除→确认删除自身测试会话
````


预期与判定：

- 示例只填入不发送；版本提示一直可见
- 当前会话与各自草稿可恢复且不串；删除取消不丢历史，确认只删指定会话
- 桌面和390px手机可操作

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`tests/check_v1_ui.py` → `main`
- 复用：`tests/check_v1_flow_review.py` → `run`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A21 · 在途草稿、断线恢复和失败反馈

方式：页面操作/受控回放。预期来源范围：见各步骤/判据。

输入：

1. 输入

````text
暂停发送确认→输入下一轮草稿→恢复确认→刷新；轮询一次503后恢复；提交503不执行
````


预期与判定：

- 已发送内容只提交一次，新输入不被清空且刷新保留
- 断线提示可见，恢复完成后清除过期警告
- 提交失败保留输入，不生成成功消息、不自动重发；切换会话不污染其他输入

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`tests/check_v1_flow_review.py` → `run:draft/transient/reject`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## A22 · 答案依据及不完整/失败状态

方式：页面操作/受控回放。预期来源范围：见各步骤/判据。

输入：

1. 输入

````text
回放已存真实组件答案/证据不足结果；注入generation_incomplete、execution_failed、source_integrity_failed状态
````


预期与判定：

- 答案来源可展开并显示版本、原文位置和条件
- 不完整/失败不能标完成，不展示半截为成功答案
- 这类回放仅验证UI状态展示，不能代替真实LLM/Dense证据忠实度检查

知识/行为依据：

- [产品契约 · V1_PRODUCT_CONTRACT_20261024.md](E:/AI-Research/DorisRAG/reports/V1_PRODUCT_CONTRACT_20261024.md)
- 复用：`tests/check_v1_flow_review.py` → `run:answer_and_failure_states_replay`
- 复用：`tests/test_query_pipeline.py` → `ContextAndAnswerTests`

本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。

## 复用时排除的旧判据

- `eval/holdout_cases.jsonl#unique_document_conflict`：旧要点将1.2能力引入与2.1默认行为边界看作不一致，既有审阅已指出不必然矛盾；不据此强制判真实冲突。
- `eval/round2_cases.jsonl#version_conflict`：这是旧显式来源选择/API兼容用例，不可替代正式Session本轮版本优先与纠正契约。
- `experiments/ragas_20261023/final_run/questions.json#Q10/Q11/Q12/Q13/Q17/Q22`：重复、跨版本相同文本或弱关联并列，不为多跳标签重复增题或加分。

# 本地资源与测试依赖

`config.json`采用相对目录，运行时由项目根解析：

| 路径 | 用途 | 源码仓库处理 |
|---|---|---|
| `doris-website-master/doris-website-master/` | 原始文档及引用原文读取 | 不上传；本地保留 |
| `data/models/bge-base-zh-v1.5/` | 同构建匹配的模型权重/tokenizer | 不上传；本地保留 |
| `data/round1_20261023/processed/` | documents、chunks | 不上传；本地保留 |
| `data/round1_20261023/builds/`、`index/`、`manifests/` | 向量/FAISS/metadata、活动指针及验收身份 | 不上传；本地保留 |
| `reports/round1_20261023/build_checks.json`、`run_status.json` | Web就绪门禁直接读取 | 只保留原记录；须与实际构建一致 |
| `data/sessions.sqlite3` | 用户会话和请求快照 | 仅本地；不能活跃写入时只拷主文件冒充一致备份 |
| `.env`、`logs/` | 本地凭据、日志与Trace | 忽略并保留在本地 |

## 新环境的现有限制

prepare/verify/build会写入数据；本次没有执行它们。原 `tests/check_knowledge_build.py`还读取：
- `reports/read_only_survey_20261023/source_manifest.jsonl`；
- 模型目录下的原下载metadata；
- 原始文档的具体内容与SHA。

`web.index_ready`还固定读取上述round1门禁目录。仅下载同名模型或运行build不能保证全套门禁自动满足。本次整理没有改业务实现，也没有提供未经验证的自动重建指令或伪造新验收记录。迁移时须保留/核对原材料，或另行安排可复现构建改造。

## 原测试的本地依赖

| 测试 | 除仓库自带夹具外的资源 |
|---|---|
| `test_evaluation.py`、`test_eval_execution.py` | `reports/quality_baseline_20261025/`；前者另需partial_confirmation下标注 |
| `test_focused_logic.py` | focused_logic_fix下routine_original_conversation.json、a13_frozen.json |
| `test_version_targets.py`部分方法 | 特定历史Trace及version_logic_fix下a13_input.json |
| `test_knowledge_processing.py`部分方法 | 原始Doris文档 |
| `test_pipeline.ImportedDataTests` | 已prepare的数据及原始文件；没有数据时按原测试Skip |
| Ragas适配专项 | 可选Ragas环境；不与产品依赖强行混装 |
| 原真实验收脚本/评测命令 | 历史baseline、输入、正式模型调用授权与预算 |

这是静态依赖清单，不等于每项已在纯源码克隆执行。所有原测试与夹具未修改，Gold未改。仓库中的CONV006离线冻结仅验证对应机制，不代替真实模型六轮验收。

历史目录在本地归档中保持相对路径；日志不在历史ZIP中，以原工作区本地备份为准。恢复含用户材料的文件前，按实际授权处理其内容。


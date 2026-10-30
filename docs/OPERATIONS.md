# 部署、使用与维护

本说明基于当前代码和保留的运行记录，核对日期2026-10-29；不是本次重新部署成功证明。质量状态见 [QUALITY](QUALITY.md)。

## 环境与首次准备

已验证环境为 Windows、Python 3.11（历史环境记录3.11.9）、本机CPU；不需要Doris集群或GPU。项目是独立目录，无可用Git提交身份，复现以文件SHA和运行manifest为准。没有经验证的跨平台安装或最低内存/吞吐SLA。

现有工作目录：`E:\AI-Research\DorisRAG`。已有解释器和依赖可直接复用，不要为启动重复安装或重建索引。新环境可在独立虚拟环境安装主依赖：

```powershell
Set-Location E:\AI-Research\DorisRAG
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

[requirements-lock.txt](../requirements-lock.txt)固定主运行包；[requirements.txt](../requirements.txt)为宽范围约束，不能保证同样版本。主lock不包含所有开发/浏览器/Ragas可选依赖，不能保证仅装它就能运行全部评测测试；不要自动向主环境安装历史Ragas大量依赖。原生适配器环境缺失时明确SKIP。可选实验环境版本和来源在对应实验报告中，不混装到产品环境。

必须准备原始网站目录、完整活动索引及 `data/models/bge-base-zh-v1.5` 的同一模型权重/tokenizer。`local_files_only=true`，启动不会自动下载缺失模型。模型输入512 tokens、向量768维；不能以另一BGE型号或相同维度的不同权重替换已有索引的编码器。

## 配置

| 位置 | 当前值/规则 |
| --- | --- |
| [config.json](../config.json) | source_root、data_dir、BGE、local_files_only、512/340 token预算、batch32、threads8、Top-K5、hybrid_precise、默认4.x |
| `.env` / 进程环境 | `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；不在文档和日志中复制密钥 |
| 已授权服务回退 | 独立LLM组三项均未设置时，整组使用 `AGENT_MODEL_API_KEY` / `AGENT_MODEL_BASE_URL` / `AGENT_MODEL_NAME`；不混用两组 |
| 优先级 | dotenv不覆盖已有进程环境；独立LLM组只填一部分视为未完整配置，不偷偷拿另一组补齐 |

新环境需要时手动复制 .env.example（本地归档：../.env.example）并填写完整三项；已有 `.env` 不覆盖。BASE_URL填写供应商兼容接口基址，具体路径以供应商配置为准，不假设任何地址都应加 `/v1`。配置修改后重启空闲服务才有可靠生效依据。

LLM配置不仅用于最终生成，也用于复杂输入提取及必要的会话关系判断。因此`search`虽然不生成答案，复杂输入仍可能调用模型；旧.env示例“仅用于真实答案生成”的注释不能用来判断是否收费。缺配置时简单证据查询可用，复杂提取不能保证继续；不会把证据拼接伪装成模型答案。

## 启动与停止

在已准备好的环境中：

```powershell
Set-Location E:\AI-Research\DorisRAG
python -m doris_rag serve --port 8765
```

或双击 [start_v1.cmd](../start_v1.cmd)。打开 [本机页面](http://127.0.0.1:8765)。直接执行`serve`不带端口时默认8000，项目启动脚本使用8765。仅绑定127.0.0.1；当前不提供公网部署鉴权方案。使用虚拟环境时将上述python替换为对应解释器，避免装包/启动解释器不一致。

页面`/api/status`会触发本地检索预热：`index_ready=true`表示构建门禁通过，`retrieval_ready=true`表示预热完成，`llm_configured=true`只表示配置完整，**不证明服务连通或答案正确**。首次预热耗时与热请求分开记录。发现已有服务先核对端口和运行身份，避免重复启动；本次文档工作没有重启现有服务，也没有验证其已加载最新代码。

正常停止可在启动终端Ctrl+C，尽量等待当前请求完成。强制关闭/断电会中断内存任务；下次创建SessionStore将遗留处理中回答标为interrupted。没有自动恢复上游推理执行，用户可按界面明确重试。不要运行构建或实例化存储对象当作只读状态检查。

## 使用

首页引导只填示例；编辑后发送。支持多行输入和Ctrl/⌘+Enter。一次聊天可完成多个任务；“补充/纠正/继续”接续活动目标，明确新问题切换主题。指定版本可以直接写在问题里；默认4.x，临时比较不改环境，证据不足不扩大范围。

单条HTTP输入上限32,000字符，但同一活动任务原材料合计最多8项/30,000字符；超限会要求精简或新建，不自动丢弃旧材料。历史消息保留与本轮送模上下文不是同一范围。

左侧新建、改名、切换、删除；刷新恢复当前会话和草稿。删除有确认，空白新会话会复用/隐藏避免堆积；服务器不为历史对照删除旧空会话。答案引用可查看版本、章节与本地原文。长SQL/表格可展开。普通界面隐藏内部ID，Trace通过接口诊断字段和文件定位，不应告诉用户“每条回答都直接显示完整Trace正文”。

处理中、需要澄清、证据不足、提取失败、服务失败分别反馈。显式重试只适用于最后一个允许重试的失败回答，不复制用户消息；同一消息有新attempt。重试沿用请求快照但使用当前代码/模型/索引，不能当成原运行的精确重现。系统不自动无限重试。

常用CLI：

```powershell
python -m doris_rag search "Doris 3.x 能使用 VARIANT DOC mode 吗？" --version 3.x
python -m doris_rag ask "PI() 返回什么？" --version 4.x
python -m unittest discover -s tests -v
```

`--version`支持auto/4.x/3.x/2.1/all；all是明确跨版请求，不应成为默认值。CLI search/ask无Session；正式多轮须走会话HTTP/UI。旧`python -m doris_rag evaluate`与V2 `python -m doris_rag.evaluation`不是同一入口，命令见 [eval/README](../eval/README.md)。任何带真实问答的批量命令先确认输入、预算、新输出目录，不为验证安装成功而运行旧题目生成脚本。

## 日志与定向诊断

- `logs/runtime/server-YYYY-MM-DD.log`按北京时间记录产生日期追加，保留Uvicorn控制台输出；跨日首条记录换文件，空闲日不生成空文件。
- `logs/traces/`中每请求有增量JSONL，结束时有JSON快照及共享代码/配置身份清单。使用`telemetry.load_trace`读取，不能仅看旧JSON而漏掉后续JSONL；二者和身份文件一起保留。
- **普通请求默认轻量Trace**，不保存问题/文档正文、完整messages或原始响应。要查实际送模遗漏，在下一次已授权的定向请求中显式设置`diagnostic=true`；这只能记录该次，不能补回历史未保存内容。API示例载荷：`{"query":"待验证的原问题","diagnostic":true}`，Session turns同样支持。
- 完整诊断对已知密钥/令牌等脱敏，但仍含用户技术材料，不是通用隐私清洗保证。不要公开.env、会话库、原始日志/Trace；日志无自动清理和24小时过期，需自行管理磁盘与备份。
- 先确认代码/config/index身份，再对比实际任务范围、各通道排名、补取和messages。最终引用只是送模证据子集。`model_calls`才包含前置关系调用，pipeline计数可能较小；父子阶段不相加。

| 现象 | 首先核对 |
| --- | --- |
| 知识库未就绪 | 活动current.json、build manifest、`reports/round1_20261023/build_checks.json`和run_status是否同ID且通过；本地模型是否存在 |
| 一直预热/预热失败 | 运行日志、模型/索引文件和依赖；不通过反复构建解决 |
| LLM未配置或连接/TLS/超时 | 配置组完整性、供应商状态/网络、Trace错误类型；不输出密钥、不把异常当Doris事实 |
| 忙/旧版本冲突/队列满 | Session busy/revision、是否已接受请求；刷新确认，避免自动重复POST |
| 回答错误或漏证据 | 先查原文→片段→排名→选位→实际messages→答案，区分检索/上下文/生成/源资料缺口 |
| Trace缺失/不完整 | diagnostics.save_status/recovery_path、日志尾行及磁盘；轻量模式缺正文属记录边界 |

## 数据维护、备份与迁移

启动现成系统**不需要**运行prepare/verify/build。它们的含义：

```powershell
python -m doris_rag prepare
python -m doris_rag verify
python -m doris_rag build
```

以上是维护构建顺序，都会产生写入，不是本次建议立即执行的健康检查。应先改解析/切片并校验，再编码，避免重复构建。完整身份匹配才允许断点恢复；模型或数据变化不能拼接旧向量。保留旧有效构建，采用独立数据目录并明确验收/切换；当前Web门禁包含固定报告路径，须确保新验收记录与活动ID一致，不能只改data_dir就宣称完成发布。本次没有执行上述命令。

备份至少包括：原始网站、config及安全保存的.env、本地模型、当前完整build及指针/清单/门禁报告、`data/sessions.sqlite3`、日志/Trace及身份文件、eval/测试夹具、reports和experiments中的原始结果。SQLite用其backup机制或停止写入后复制，不能在活跃写入时只拷主db并宣称一致快照。不要为了检查会话数量而构造SessionStore，初始化会恢复busy状态。

部分产物包含原机器绝对来源路径。迁移后先核对路径、SHA、编码器指纹及门禁；仅移动模型或修改模型名不能保证零重建。若来源路径无法成立，应在保留旧完整产物的前提下建立新的受验收产物；没有已验证的一键异地迁移流程。备份及迁移恢复成功尚无本次新实测，不能列为已验收能力。

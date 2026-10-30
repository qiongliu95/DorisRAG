# DorisRAG

基于本地 Apache Doris 中文官方文档的问答系统，支持版本范围、SQL/日志材料、多轮会话和可追溯引用。使用 Python/FastAPI、原生网页、BGE-base-zh-v1.5、FAISS、BM25 与 RRF。

本目录是源码发布副本，不包含真实密钥、用户会话、运行日志、模型、原始文档或已构建索引。当前功能仍有未关闭缺陷，不能视为全部验收通过。现行产品约定与质量边界见 [项目文档](docs/README.md)。

## 仓库内容

- `doris_rag/`、`ui/`：产品源码与页面。
- `config.json`、依赖文件、`.env.example`、`start_v1.cmd`：运行配置与启动入口。
- `tests/`、`eval/`：原测试、夹具、Gold和评测规则，保持原字节和断言。
- `docs/`：现行说明；原先指向未公开历史产物的链接已标明“本地归档”。
- `reports/round1_20261023/build_checks.json`及`run_status.json`：现有Web门禁直接读取的两份记录，不能作为普通报告删掉。它们只对应原活动构建，不代表新机器已通过验收。

## 本地启动条件

使用 Python 3.11；依赖参考 [requirements-lock.txt](requirements-lock.txt)。创建虚拟环境后按项目目录执行：

```powershell
python -m pip install -r requirements-lock.txt
Copy-Item .env.example .env
```

按所用兼容接口填写 `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`。复杂材料提取和关系识别也可能调用该模型，检索命令并不一律零模型费用。

**首次克隆还不能直接问答。** 需要另备：
1. `config.json`指定位置的原始Doris中文文档快照。
2. `data/models/bge-base-zh-v1.5/`的完整匹配模型/tokenizer；当前配置只允许本地加载。
3. `data/round1_20261023/`中的处理数据、manifest、完整活动构建和指针，以及与其一致的门禁记录。

现有产物带原机器绝对来源路径；异地复制或重建需要核对路径、SHA、模型指纹和门禁。构建验收脚本还引用原始来源清单及模型下载元数据，尚无经验证的无历史资料一键部署流程。不要修改门禁为passed来绕过检查。[本地资源说明](docs/LOCAL_ASSETS.md)列出所需材料与现有耦合。

资源就绪并经核对后：

```powershell
python -m doris_rag serve --port 8765
```

或运行 `start_v1.cmd`，浏览器打开 http://127.0.0.1:8765 。仅供本地使用，当前没有公网鉴权部署方案。

## 测试与评测

完整回归入口保留：

```powershell
python -m unittest discover -s tests -v
```

部分测试依赖未公开的历史输入/Trace、原始文档或本地数据；纯源码克隆不保证完整discovery通过。具体依赖见 [测试资源说明](docs/LOCAL_ASSETS.md)，无需为获得绿色结果删除测试、改Gold或降低断言。

可先运行不依赖历史运行材料的任务契约专项：

```powershell
python -m unittest discover -s tests -p test_generation_task_contract.py -v
```

真实模型验收、Ragas环境及评分须另按 [eval/README.md](eval/README.md) 与 [AGENTS.md](AGENTS.md) 的授权和规则执行。

## 历史资料与许可

[ARCHIVE.md](ARCHIVE.md)说明哪些历史/研究资料未进入仓库及其依赖边界。历史失败不因源码整理而关闭，也不沿用旧分数宣称当前通过。

本次整理未添加开源许可证，代码授权方式待项目所有者确定；第三方文档、模型及题源各自遵循原有许可与来源要求。不要将此仓库名或Apache Doris资料来源理解为整个项目自动适用某个许可证。


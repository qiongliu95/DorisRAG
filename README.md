# DorisRAG

基于 Apache Doris 中文官方文档的本地问答系统，支持 4.x、3.x、2.1 文档、混合检索、多轮会话和来源引用。

## 新电脑安装（Windows 10/11，64 位）

建议 16 GB 内存，至少预留 10 GB 磁盘。首次安装需要联网下载 Python 依赖与约 627 MB 运行资源；后续本地检索不需要重新下载。最终回答及部分复杂问题理解需要可用的兼容 LLM 接口。

1. 安装 [Python 3.11 x64](https://www.python.org/downloads/windows/)，安装时勾选 **Add python.exe to PATH**。安装 [GitHub CLI](https://cli.github.com/) 后重新打开终端。若使用下面的克隆命令，还需安装 [Git for Windows](https://git-scm.com/download/win)；使用源码 ZIP 则不需要 Git。
2. 在 PowerShell 执行以下命令，浏览器中登录有此私有仓库访问权的 GitHub 账号：

```powershell
gh auth login
gh repo clone qiongliu95/DorisRAG
cd DorisRAG
.\setup.cmd
```

建议放在较短的本地路径（如 `C:\AI\DorisRAG`），避免过长的目录名。也可以从 GitHub 下载源码 ZIP 并解压，然后双击 `setup.cmd`。私有 Release 仍需要先执行 `gh auth login`。

3. 首次安装可能需要较长时间，依赖安装和资源校验期间请保持窗口打开。等到窗口显示 **Runtime restoration and retrieval acceptance PASSED** 和 **READY**。脚本会建立 `.venv`、安装锁定依赖、下载匹配模型/文档/预构建索引、校验 SHA256，并自动修正新电脑的来源路径。无需自行寻找旧报告、手工搬模型或重算索引。不要在安装前修改 `config.json`。
4. 用记事本打开安装时自动创建的 `.env`，填写自己的接口配置并保存：

```dotenv
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://你的服务地址/v1
LLM_MODEL=你的模型名称
```

接口需兼容 OpenAI Chat Completions；地址按服务商要求填写。不要把真实 `.env` 提交到 GitHub。

5. 双击 `start_v1.cmd`，保持窗口打开，在浏览器访问 **http://127.0.0.1:8765**。首次加载模型/检索器需要等待。退出时关闭该窗口或按 Ctrl+C。以后只需重复这一步。

## 常见情况

- `py -3.11` 找不到 Python：安装 Python 3.11 x64 并重新打开终端；使用该版本，不要随意改为其他版本。
- 下载提示 404/无权限：运行 `gh auth status`，确认账号能访问 [私有运行资源](https://github.com/qiongliu95/DorisRAG/releases/tag/runtime-v1)。
- 下载中断或安装失败：保留错误信息，重新运行 `setup.cmd`；完整且 SHA 匹配的压缩包会复用，损坏缓存重新下载。
- `torch` 提示 DLL 加载失败：安装 Microsoft [Visual C++ x64 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe)，然后重新运行安装。
- 依赖下载失败：确认新电脑能访问 PyPI 和 GitHub；安装窗口会保留具体错误。
- 网页可以打开但回答提示模型配置错误：核对 `.env` 的三项配置、服务余额和模型可用性。安装验收不调用付费回答模型。
- 安装后不要直接搬动整个目录。路径绑定当前安装目录；迁移到另一台电脑时重新下载源码并运行安装。
- 重复安装会验证现有安装，不覆盖 `.env` 和用户会话。改过模型/数据/config 的开发目录应另建干净目录安装。

## 安装验证的范围

安装成功表示运行资源完整、模型可加载、真实向量与索引一致、三个文档版本检索及原文回读通过。它不表示所有回答正确，也不表示现有产品缺陷全部关闭。没有真实 API 配置时不会假称完成问答端到端验收。

安装证据保存在 `data/round1_20261023/installation.json` 与 `reports/round1_20261023/build_checks.json`。模型、索引和文档通过私有 Release 分发，源码仓库不直接保存大文件和本地会话。

## 开发与测试

[项目文档](docs/README.md)、[资源说明](docs/LOCAL_ASSETS.md)、[评测规则](eval/README.md)、[回归约束](AGENTS.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_generation_task_contract.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

完整 discovery 中部分历史回放测试还依赖本地归档的评测输入/Trace；安装包提供产品运行资源，不将它们伪造为新机器验收。不要为获得绿色结果删除测试、改 Gold 或降低断言。

## 来源与许可

Doris 文档来自 Apache Doris 官网快照，发行包保留其 LICENSE；BGE-base-zh-v1.5 官方模型卡标注 MIT，随资源保留模型卡。来源分别见 [Apache Doris](https://github.com/apache/doris-website) 与 [BAAI 模型](https://huggingface.co/BAAI/bge-base-zh-v1.5)。本项目代码尚未指定开源许可证，仓库当前为私有。历史材料整理范围见 [ARCHIVE.md](ARCHIVE.md)。

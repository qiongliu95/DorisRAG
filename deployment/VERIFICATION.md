# 安装交付验证（2026-10-30）

在同一台 Windows x64 机器的新建 Python 3.11 虚拟环境和独立目录验证，未使用原项目的 Python 包环境。首次依赖安装后因 C 盘空间耗尽，将独立验证目录迁到 E 盘完成；这不是另一台物理电脑或全新 Windows 的实测。

已通过：

- 从 PyPI 安装依赖，`pip check` 无冲突；实际解析出的 52 项依赖固定于 requirements-lock.txt。
- 完整 GitHub Release 下载（GitHub CLI 及不带认证的 HTTP 两种入口均实测通过），627 MB 资源包的大小、SHA256 与固定清单一致，GitHub 服务端 digest 也一致。
- 最终安装器恢复文档、模型、索引，自动迁移来源路径，生成与新路径匹配的构建标识。
- 原有 tests/check_knowledge_build.py 通过：4109 篇来源 SHA、51391×768 向量与 FAISS 顺序、metadata 与 chunk 一致、模型权重/tokenizer 指纹、三个来源版本的真实检索、代码/表格/分页上下文回读。errors 为空。
- HTTP 服务实际启动：页面和脚本 200，检索预热 ready，原文接口 200，会话创建 201 且列表可读。验证时移除了模型服务环境变量，llm_configured=false，没有付费模型调用。
- 模拟找不到 GitHub CLI 时，最终安装器仍成功完成现有资源验收。公开资源由 Python 标准库直接下载，私有仓库才需要可选 CLI 认证。
- 再次执行最终安装器成功，复用现有资源；`.env` 和已有会话数据库文件的 SHA 保持不变。
- 8 项原有 generation_task_contract 回归通过。损坏下载的故障注入检查通过：拒绝损坏资源，历史 completed 门禁改为 checking；该项替代了网络/pip，不冒充真实下载验收。
- Git 暂存区 config.json 的原始字节 SHA 与资源清单相同；.gitattributes 阻止换行转换改变该约束。

首次试装遇到的中文 Windows pip 编码问题已通过显式 UTF-8 修正。C 盘不足导致的中断没有计作成功；最终在空间充足的独立目录重新执行并通过。安装器已增加首次安装的空间检查。

未覆盖：另一台物理电脑、其他 Windows/驱动组合、Linux/macOS、真实 API 最终回答质量与评测 Judge。未运行依赖本地归档材料的完整 unittest discovery；没有删除测试、改 Gold 或放宽断言。此次没有改动应用源码、前端、原测试和评测内容。

每次安装的实际结果在本机 data/round1_20261023/installation.json 和 reports/round1_20261023/build_checks.json；安装成功仅证明资源/运行链路及上述检索检查通过，不关闭已有产品缺陷。

# 本地运行资源

新电脑按仓库根目录 README 执行 `setup.cmd`。运行资源从GitHub Release `runtime-v1` 获取，固定大小与 SHA256 见 `deployment/resources.json`。

资源包含匹配的 CPU embedding 模型、4109 篇三版本中文源文档、51391 个 chunk、预构建 FAISS 索引与向量、原始来源哈希清单和第三方来源文件。资源不含真实 `.env`、用户会话或开发运行日志。

`deployment/restore_runtime.py` 只修改来源路径字段，重新计算受路径影响的文件哈希和构建标识，并实际执行原有 `tests/check_knowledge_build.py`。清洗/切分验证记录注明其继承来源；运行验收不是回答质量验收。

安装不需要完整网站源码、网站依赖、原网站 ZIP 或全量历史报告。完整 unittest discovery 中引用历史评测输入与原始 Trace 的测试仍需项目本地归档；这类资料不属于产品启动依赖。

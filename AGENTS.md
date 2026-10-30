# 回归约束

遵循用户的全局最小修改要求，不修改 Gold 或降低断言。

修改 Session、Query Understanding、Retrieval、Context 或生成任务契约后，必须执行现有一键离线回归：

```powershell
python -m unittest discover -s tests -v
```

其中 `tests/fixtures/session_relation_cases.json#CONV006` 是六轮原始连续对话，必须同一 Session 顺序执行，不可拆题或注入正确活动状态。`tests/test_session_relation.py` 和专项回归测试由 unittest discovery 自动执行；离线夹具不能冒充真实模型验收。真实验收沿用项目授权和调用预算，不通过反复生成择优，不自动调用 Judge。

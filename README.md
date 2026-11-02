# Continuum（续）

常驻在操作系统里的 AI 幕僚长：看得到你此刻在做什么，记得过去发生了什么，并能替你长期负责一部分工作。

> 用户切换的是软件，AI 维护的是同一个连续的工作世界。

## 组成

- **Super-agent**：云端 Router + Worker + Watchdog，事件驱动，长期推进目标。
- **OS Runtime**：设备侧用户态服务，按事件感知屏幕与应用，在授权范围内执行操作。
- **Memory**：Personal Context Graph + 事件日志 + 事实偏好 + 技能。
- **Goal Engine**：把长期责任存在数据库和状态机里，模型不当事实来源。
- **Policy Engine**：能力令牌、风险分级审批、不可信标记、审计与撤销，贯穿所有层。

![整体架构](docs/images/architecture.png)

## 文档

- [项目大纲](docs/outline.md)：定位、要解决的问题、切入场景、目标、相似项目、架构、模块设计、技术选型、路线图、风险。
- [框架设计（草案）](docs/framework.md)：用 Python 复刻 dsh-superagent 的核心思想，内核小、扩展点多。

## 开发

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## 状态

- F0 内核已完成：领域模型、Engine 五部分、Gate 规则链与标准 Gate、研判校验、策略变更影响、内存仓库，50 个测试。
- 下一步 F1：SQLite、Effects 队列、Outbox、调度循环、插件注册表。
- 首个切入场景拟为制造业 BOM 报价与采购跟进。

`core` 和 `policy` 移植自 dsh-superagent（MIT），见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

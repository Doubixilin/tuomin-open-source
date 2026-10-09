# 核心流程和代码入口

以 `examples/minimal_demo.py` 为起点：

1. `detectors/rules.py` 按格式发现候选值；`detectors/dictionary.py` 匹配声明的实体与别名。
2. `fusion.py` 和 `spans.py` 处理重复与重叠，保留可追踪的跨度。
3. `redactor.py` 将选中的原文跨度替换为占位符；`mapping.py` 管理对应关系。
4. `refill.py` 验证未知、缺失或变形占位符；验证通过才按选定回填合同恢复原值。

以上路径相对 `src/tuomin_gateway/`。原值映射与送给外部模型的脱敏文本有不同的访问边界，不能一起发送。

进一步阅读：

| 模块 | 用途 |
| --- | --- |
| `session.py` | 会话内稳定占位符、检测器就绪与编排 |
| `profiles.py` / `policy.py` | 哪些值脱敏、泛化、阻断或保留 |
| `store.py` / `platform_security.py` | 平台存储保护与文件权限 |
| `vault.py` / `namespace_vault.py` | 不透明映射句柄、命名空间和可信恢复 |
| `service/v1.py` | 带能力权限的 API |
| `service/proxy.py` | 经显式配置的上游代理 |
| `document/` / `service/v1_documents.py` | 文档解析、工作台与恢复流程 |
| `mcp_stdio.py` | MCP 接入边界 |
| `guard/` / `audit.py` | 注入与秘密模式检测、安全审计摘要 |
| `licensing/` | 可选离线授权机制研究；社区运行默认关闭且不包含发行身份 |

接口分层：优先研究 `/api/v1`；旧 `/redact`、`/refill` 和会话接口为兼容路径。不要把旧接口的回填约束当成所有接口的统一权限模型。

本公开版不提供包含模型权重的桌面分发流程，也不为历史评测结果背书。应按自己的输入、配置与版本重新验证。

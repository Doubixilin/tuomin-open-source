---
name: tuomin-guard
description: 处理可能含敏感信息的材料前，先经 Tuomin 本地脱敏网关检查与脱敏，只把 egress_allowed=true 的结果交给模型；回填永远引导用户去本机 Tuomin 界面完成。
---

# Tuomin 脱敏守卫

当任务涉及合同、项目材料、人员名单、联系方式、账号、地址等可能含敏感信息的内容，且当前会话模型走 Tuomin 网关（或用户要求先脱敏）时，按本流程执行。

## 前置检查（每次任务开始时做一次）

1. 调用 `tuomin_readiness`（app_id 用用户指定的项目 app，缺省用环境约定的默认 app）。
2. `ready=false` 或工具不可用时：**停止并明确告诉用户"脱敏通道未就绪"**，不要降级为直接处理原文。

## 脱敏与使用

3. 需要把材料交给云端模型前，调用 `tuomin_redact_text`（结构化字段用 `tuomin_redact_values`）。
4. **只有 `egress_allowed=true` 的 `masked_text` 才允许进入你的上下文或发往任何模型**；否则停止并报告阻断原因（`error.code`）。
5. 保留返回的 `job_id`：它不具备恢复能力，仅用于用户在本机 Tuomin 界面定位任务。

## 占位符纪律（违反会破坏回填）

6. 看到 `<ORG_001>` 这类占位符时：**绝不编造原值、绝不"修复"或改写占位符格式、绝不删除**。
7. 不要主动把含占位符的文本再次发给任何脱敏端点（会触发 409）——占位符内容在 agent 侧的下游使用应保持原样传递。
8. 占位符文件落盘后，提醒用户：该文件留在工作区会让后续会话读到它并触发 409，建议及时回填或移出。

## 回填（永远不要自己做）

9. 你**没有也不应调用**任何回填/mapping/解锁能力。需要还原时，引导用户：打开本机 Tuomin WebUI 的「回答回填」或「回填还原」，凭恢复包口令完成；或在「可信预览」解锁查看。WebUI 端口随网关启动方式变化（CLI 默认 8765、打包 App 动态、launchpad 如 8775），以网关启动日志里的 `WebUI at /ui` 地址为准，不要写死端口。
10. 用户问"怎么还原"时给出上一步路径；不要尝试用 job_id 直接还原。

## 边界（诚实告知用户）

- 只有走 Tuomin 自定义模型的会话被脱敏；内置模型、MCP 连接器、IM 远程通道不在保护范围。
- 图片内容不脱敏；扫描件 PDF 不支持 OCR。
- `egress_allowed=true` 只表示当前配置与扫描通过，不是零漏检承诺；重要材料请用户人工复核占位符覆盖。

## 配置（由用户/管理员完成，不在本文件保存任何密钥）

- 本 Skill 不含任何 token。MCP server 配置（`python -m tuomin_gateway.mcp_stdio`）与 capability token 由用户在本机环境变量设置：`TUOMIN_GATEWAY_URL` / `TUOMIN_AGENT_REDACT_TOKEN` / `TUOMIN_AGENT_NAMESPACE_TOKEN` / 可选 `TUOMIN_AGENT_APP`。

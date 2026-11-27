# 桌面工作台图文指南

Tuomin 的 macOS 菜单栏和 Windows 托盘提供系统操作，窗口内的文本、文件、词典与工具接入共用一套工作台。可以先下载最新发行版，或从源码运行 `python packaging/launcher.py --demo`，用自带的合成合同和台账练习。[下载发行版](https://github.com/Doubixilin/tuomin-open-source/releases/latest) · [从源码启动](../README.md#从源码运行桌面工作台)

## 开始

首次启动时选本机词典，按需开启登录启动和全局热键。先把项目别名、简称、人名和单位加入词典。Lite 版使用规则和已配置的词典，未登记的姓名和组织可能保留在正文中。

| 开始页 | 首次设置 |
| --- | --- |
| ![工作台开始页的文本、文件和工具接入入口](manual/img/01-home.webp) | ![首次设置词典来源、快捷键和工具接入](manual/img/02-onboarding.webp) |

## 剪贴板

1. 先在「设置 → 剪贴板使用的词典」确认来源。全新 `--demo` 默认选「合成合同演示」，正常桌面首次使用「本机工作台」。在文档里**复制原文**。此时系统剪贴板仍是原文。打开菜单栏或托盘的「脱敏剪贴板」，或按默认热键（macOS **⌥⌘D**，Windows **Ctrl+Alt+D**）。
2. Tuomin 此时读取剪贴板文本，按当前配置检测并建立本次本机映射。成功后，**由 Tuomin 把占位符文本写回系统剪贴板**。到 AI 应用手动粘贴，检查输入框再发送。普通脱敏热键不会代你发送消息。
3. AI 回答后，复制带占位符的回答，触发「回填剪贴板」（macOS **⌥⌘R**，Windows **Ctrl+Alt+R**）。Tuomin 校验当前会话里的占位符，成功时把恢复后的回答写回剪贴板。这时内容已经含原值，请粘贴到受信任的文档。
4. 在菜单的最近结果或原值对照表中复查。如果 AI 改写了标记、标记来自另一会话、检测器未就绪，或等待期间剪贴板已被别的程序改动，普通动作会提示原因并保留剪贴板当时的内容。

下面的图来自隔离合成会话。实际剪贴板控制器和本机服务把「虚构星橙工程有限公司」「许星舟」换成占位符，再把回答中的这两个占位符恢复。为避免接触个人剪贴板，拍摄时以进程内文本缓冲区代替系统剪贴板；对照表与阻断提示由工作台实际渲染。

![同一剪贴板会话内的机构、人名与合同编号映射，均为合成值](manual/img/walkthrough-clipboard-mapping.webp)

![用当前会话以外的占位符回填，工作台指出未匹配的标记](manual/img/walkthrough-clipboard-blocked.webp)

**什么时候会自动粘贴？** 「脱敏选中文字」会先模拟复制，成功后把脱敏文本留在剪贴板供你粘贴。「选中脱敏并替换」和「选中回填并替换」才会在目标窗口执行粘贴，完成后尝试恢复操作前的剪贴板文本。先在设置中为这些动作绑定热键。macOS 需允许辅助功能；Windows 需目标程序允许模拟复制粘贴。操作期间保持目标窗口与选区不变。

**退出和清理。** 关闭工作台窗口后可继续从菜单栏或托盘使用。完全退出会停止服务和热键。需结束原值关联时，请在菜单中结束并清除当前会话；系统剪贴板、其他程序的粘贴历史和你已保存的文件仍需分别处理。

![设置中录制普通剪贴板动作和选中文字动作的快捷键](manual/img/12-settings.webp)

## 文本预览

打开「文本脱敏」，粘贴一段文字，选择档位后点「脱敏」。左栏标示原文中被识别的范围，右栏显示替换结果。下面的轻度预览识别了测试邮箱和固定电话，同时保留了核对箱号、拍摄封条的工作要求。预览不会保存本次映射；需要回填时请选择剪贴板、文件或会话流程。

![轻度规则预览中的合成邮箱、固定电话和原文对照](manual/img/walkthrough-text-diff.webp)

[查看另一组合同预览，含本地 NER 的截图](manual/img/03-try.webp)

## Word、Excel、PowerPoint

打开「文件脱敏」，选择文件和词典。「解析并脱敏」便于读正文、看标签和出站检查；「导出原格式脱敏文件」生成保留受支持版式的 Office 副本。出站面板出现阻断时，先解决配置或内容问题。**给别人看之前打开生成的文件**，尤其要核对一般文本、表格、备注或批注，以及无法检测的图片。

| 文件 | 建议先核对 | 现成对照 |
| --- | --- | --- |
| Word `.docx` | 打开原件与脱敏件，比较段落、表格和编号；再打开回填件查机构及联系人 | [原件](../demo/office-showcase/original/service-agreement.docx) · [脱敏件](../demo/office-showcase/redacted/service-agreement.docx) · [回填件](../demo/office-showcase/refilled/service-agreement.docx) |
| Excel `.xlsx` | 查看工作表、列和单元格里的识别结果；再比较回填件中的原值 | [原件](../demo/office-showcase/original/delivery-register.xlsx) · [脱敏件](../demo/office-showcase/redacted/delivery-register.xlsx) · [回填件](../demo/office-showcase/refilled/delivery-register.xlsx) |
| PowerPoint `.pptx` | 逐页检查文本框、表格与关键版式；图片中的字需人工看 | [原件](../demo/office-showcase/original/project-brief.pptx) · [脱敏件](../demo/office-showcase/redacted/project-brief.pptx) · [回填件](../demo/office-showcase/refilled/project-brief.pptx) |

**PowerPoint 对照。** 同一页投影片保留原有布局，正文里的组织和项目名称被换成占位符。

| 原投影片 | 脱敏投影片 |
| --- | --- |
| ![合成项目简报原页](../demo/office-showcase/images/project-brief-original.webp) | ![同一投影片的脱敏副本](../demo/office-showcase/images/project-brief-redacted.webp) |

![选择合成 Excel 台账后，工作台列出文件、策略和脱敏预览](manual/img/walkthrough-spreadsheet.webp)

![合成 Word 文件处理与导出控制](manual/img/04-files.webp)

拿到改过的文件时，在「文件回填」里选最初的任务，再指定接收方传回的文件。检查新增、缺失或改写的占位符，按界面要求确认允许的变化。也可显式导出带口令的恢复包，用于跨机恢复。恢复包和原件都应留在受信任的位置。

| 选择原任务 | 查看已处理任务 |
| --- | --- |
| ![文件回填时选择相应的文档任务](manual/img/05-refill.webp) | ![合成文档与台账的本地任务记录](manual/img/08-records.webp) |

## 回答回填与本地核对

AI 可以只引用其中一个占位符，也可能多次提到同一实体。粘贴回答，选对应的文件任务或 Agent 会话，点「本地回填」。如果 AI 创造了一个映射里没有的 `<ORG_999>`，工作台会列出未匹配的标记。请核对任务和原回答。

| 回填成功 | 未知标记被拦住 |
| --- | --- |
| ![仅恢复回答中使用的机构和人名](manual/img/walkthrough-answer.webp) | ![阻断一个未知的组织占位符](manual/img/walkthrough-unknown-placeholder.webp) |

「核对与原值」可对照本机文本、脱敏 Markdown、AI 回答和恢复结果。原值区需要显式解锁，切换页面、失焦或达到空闲时间后自动锁定。真实业务的原值对照表不适合公开分享。下方素材均为虚构实体。

![合成台账的原始文本、脱敏文本、回答与回填结果](manual/img/walkthrough-local-review.webp)

| 文档任务的回答 | Agent 会话的回答 | 已解锁的合成原值 |
| --- | --- | --- |
| ![用文档任务映射恢复](manual/img/06-answer.webp) | ![在指定 Agent 会话内恢复](manual/img/06b-answer-agent.webp) | ![合成任务的原值对照](manual/img/07-preview.webp) |

## 管理词典

在「本地词典」选应用。批量导入前先预览，检查类型、别名、重复和过短的条目，确认后再写入。也可以从一份已处理的文档任务中提取候选名称，逐条勾选。别名应当指向同一实体，否则回填和指代关系都会受影响。

![预览导入虚构单位、项目和一个过短的词条](manual/img/walkthrough-import-review.webp)

| 导入前预览 | 从合成任务挑选名称 | 已保存条目 |
| --- | --- | --- |
| ![批量导入检查](manual/img/10-dict-import.webp) | ![从脱敏结果学习词条](manual/img/10b-dict-learn.webp) | ![查看组织、人名和合同编号词条](manual/img/10c-dict-entries.webp) |

## 接入 AI 客户端

「工具接入」可为受支持的客户端配置本机 MCP 工具或提供代理地址。MCP 模式由客户端调用脱敏工具，客户端能否调用需要在其自身会话里验证。代理模式要求请求实际指向对应的本机应用地址，再由网关按配置转发给上游。请检查上游凭据、检测器就绪状态和客户端显示的实际连接。单独在这些页面查看配置不会发送模型请求。

| MCP 接入与自检 | 应用对应的本机地址 |
| --- | --- |
| ![工具接入页的客户端配置和自检按钮](manual/img/09-agents.webp) | ![不同应用及模式的回环端点](manual/img/09b-endpoints.webp) |
| ![WorkBuddy 的项目与上游配置入口](manual/img/09c-workbuddy.webp) | ![应用、词典与策略档位的关联](manual/img/11-apps.webp) |

Windows 和 macOS 共用文档工作台，具体菜单、窗口外框和热键随系统变化。本页新图使用 macOS 上独立的本机服务和浏览器配置拍摄，没有调用外部模型。旧图展示合成示例，首启和快捷键设置按桌面壳的设置快照渲染；平台专属菜单操作以实际安装包为准。[工作流程](WORKFLOWS.md) · [网关配置](GATEWAY.md) · [平台存储与权限](ARCHITECTURE.md)

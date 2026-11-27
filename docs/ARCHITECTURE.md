# 技术架构与代码入口

本文对应公开源码的现有结构。图中的箭头表示主要调用或数据依赖，不表示组件均为独立服务或并行运行。除单独注明外，模块路径相对 `src/tuomin_gateway/`。

## 1. 分层与职责

```mermaid
flowchart TB
    subgraph ACCESS["接入层"]
        UI["Web 控制台<br/>service/static"]
        API["HTTP API / 模型代理<br/>service/v1 与 proxy"]
        CLI["CLI / 本地 MCP<br/>cli 与 mcp_stdio"]
    end
    UI --> ORCH
    API --> ORCH
    CLI --> ORCH
    ORCH["入口编排<br/>文本、文档、批量、会话、命名空间"]
    REG["应用注册与配置<br/>registry / profiles / policy"] --> ORCH
    ORCH --> INPUT["输入准备<br/>文档提取 / 文本及结构检查"]
    INPUT --> ENGINE["本地检测与变换引擎<br/>detectors / fusion / session / redactor"]
    ENGINE --> RESULT["输出与状态<br/>脱敏文本、检测摘要、阻断原因"]
    ENGINE --> MAP["映射管理<br/>mapping / vault / namespace_vault"]
    MAP --> STORE["平台存储<br/>store / platform_security"]
    RESULT --> DELIVERY["按入口交付<br/>本地预览 / 文件导出 / 上游转发"]
    MAP --> REFILL["可信回填<br/>refill / document/refill_flow"]
    DELIVERY -->|恢复请求| REFILL
    REFILL --> LOCAL["可信本地结果"]
    ORCH -.-> SUPPORT["旁路支撑<br/>guard / audit / jobs / inspection"]
    ENGINE -.-> SUPPORT
```

这是逻辑分层图：CLI 最小示例可以直接调用核心函数，不经过 HTTP；MCP 适配器通过本地网关 API 调用能力；模型代理在服务进程内使用检测引擎，不为每次脱敏增加一次本地 HTTP 跳转。文件和 API 复用检测与映射机制，但授权、导出限制和回填合同由各自入口处理。

## 2. 检测与脱敏流水线

```mermaid
flowchart TB
    TEXT["输入文本 + 应用策略"] --> RULE["规则检测<br/>格式与特征匹配"]
    TEXT --> DICT["词典检测<br/>声明实体、别名与标签"]
    TEXT --> NER["本地 NER<br/>人名、组织、地址候选"]
    RULE --> READY["收集候选跨度与检测器状态"]
    DICT --> READY
    NER --> READY
    READY --> REQUIRED{"必需检测器是否就绪"}
    REQUIRED -->|否| ERROR["RequiredDetectorUnavailable<br/>入口返回明确失败"]
    REQUIRED -->|是| FUSE["跨度清理与融合<br/>去重、来源优先级、重叠裁决"]
    FUSE --> POLICY["应用策略<br/>标签、风险门槛、角色与动作"]
    POLICY --> SELECT["选中跨度 + 阻断标签<br/>检测器版本与降级状态"]
    SELECT --> ONCE["单次 redactor<br/>跨度替换与本次映射"]
    SELECT --> SESSION["SessionRedactor<br/>补检已知值、稳定编号、可选泛化"]
    ONCE --> OUT["脱敏文本 + 映射 + 状态摘要"]
    SESSION --> OUT
    OUT --> ENTRY["具体入口检查状态<br/>决定预览、导出或转发"]
```

三种检测来源在逻辑上汇集，当前 `run_detection` 不表示三个独立并行服务。规则和已配置的词典属于必需检测器；NER 是否必需由策略声明。可选 NER 不可用时允许返回明确的降级信息，但文件工作台仍会因降级拒绝出站。API 使用方需要读取返回状态，不能只判断是否拿到了文本。

融合使用候选来源优先级和重叠规则，NER 候选优先级低于精确词典和规则；还包含原文位置、重复跨度和局部语义修正。它不是简单投票，也不能把重叠处理后的跨度完整性等同于敏感信息召回率。

单次脱敏与会话脱敏不是完全相同的路径。会话处理会补检已经拥有的实体值，并在锁内更新映射与编号；检测运行在锁外。别名是否共享身份还取决于身份模式，不能假定所有路径都会自动归一化全部别名。

## 3. NER 本地推理方案

```mermaid
flowchart TB
    CONF["策略启用 NER<br/>use_ner 与 ner_required"] --> SOURCE["解析本地模型位置<br/>显式目录或固定版本缓存"]
    WEIGHT["仓库不包含模型权重<br/>用户核实许可后自行准备"] -.-> SOURCE
    SOURCE --> LOAD["延迟加载与进程内缓存<br/>Transformers + PyTorch"]
    LOAD --> OFFLINE["仅加载本地文件<br/>local_files_only=True"]
    INPUT["待检测文本"] --> WINDOW["按原文切分重叠窗口<br/>优先换行与句界"]
    OFFLINE --> INFER["窗口批量推理"]
    WINDOW --> INFER
    INFER --> LABEL["模型标签映射<br/>ORG / PERSON / ADDRESS"]
    LABEL --> OFFSET["恢复原文偏移<br/>去重与实体边界修正"]
    OFFSET --> CAND["输出带来源与版本的跨度<br/>交给统一融合层"]
    LOAD -.->|不可用| STATE["由检测编排报告<br/>必需失败或可选降级"]
    classDef external fill:#fff8e6,stroke:#a66b00,stroke-dasharray:5 5;
    class WEIGHT external;
```

当前适配器面向资源索引中的中文 CLUENER 微调模型：`company / organization / government` 映射为 ORG，`name` 为 PERSON，`address` 为 ADDRESS；其他模型类别不会全部转成隐私类别。它不是通用的多语言敏感内容分类器。

当前窗口参数为 384 字符、相邻重叠 64 字符、每批最多 8 个窗口。目的在于避免长文本超出模型位置限制，并降低切断实体的风险；这些是实现参数，不是准确率或吞吐量承诺。边界扩展、跨行修复和通用词过滤属于启发式处理，仍需结合真实任务评测。

模型权重和依赖许可、文件哈希与本地配置见 [资源索引](EXTERNAL_RESOURCES.md)。预留模型接口不进入这张已实现路径图。

## 4. 授权与接口分层

| 接口族 | 主要约束 | 不应混淆的地方 |
| --- | --- | --- |
| `/api/v1/detect`、`redact`、`refill` 等 | 应用能力与独立能力 Token；回填合同与映射归属 | 获取脱敏能力不等于获取 `trusted_refill` |
| `/api/v1/documents/*` | 管理 Token、任务／恢复材料、工作台专用检查 | 使用 `/api/v1` 前缀不表示与文本 API 的授权方式相同 |
| `/admin/*` | 管理 Token 与本机 Host 检查 | 管理 Token 不是上游模型凭据 |
| `/apps/{app_id}/...` 代理 | 本机信任边界、应用策略；自动回填需要显式允许 | app_id 不构成客户端身份认证 |
| 旧 `/redact`、`/refill`、会话接口 | 兼容路径的自身约束 | 不应将其恢复模型当成 v1 的统一权限合同 |

平台存储：macOS 使用钥匙串支持的 AES-GCM，Windows 使用当前用户 DPAPI，Linux 当前存在明文开发路径；CLI 明文映射导出也需单独保管。本地存储、诊断 trace、可信预览和恢复包不是公开数据，详见 [安全边界](BOUNDARIES.md)。

## 5. 从代码与测试开始阅读

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

桌面构建流程见 `packaging/` 与 `scripts/`。源码发布包含本地模型接入，权重由运行者另行准备。

测试入口位于仓库根目录 `tests/`，包括 `test_proxy.py`、`test_documents_api.py`、`test_public_example.py` 等。模拟检测器或上游的回归验证接口和处理逻辑；真实 NER 效果、客户端兼容性与供应商联调需另行验证。

## 桌面工作台怎样调用网关

桌面包把窗口、系统适配器和本地 HTTP 服务装在一起。浏览器和原生窗口共用控制台页面。文件选择、系统剪贴板、全局快捷键和托盘通知由桌面层提供。启动器采用单实例锁，记录实际服务地址；端口冲突时客户端可通过发现文件找到当前实例。

```mermaid
flowchart TD
    Start["启动器与实例发现"] --> API["仅监听回环地址的 FastAPI 服务"]
    Start --> OS{"当前桌面平台"}
    OS --> Mac["macOS · Cocoa/WebKit、菜单栏、系统剪贴板"]
    OS --> Win["Windows · WebView2、托盘、系统剪贴板"]
    Mac --> Bridge["ShellBridge 与显式操作"]
    Win --> Bridge
    Bridge --> Clip["ClipboardController · 记录会话句柄"]
    Clip -->|"受限能力凭据"| API
    UI["共享 HTML 工作台"] -->|"本机管理请求"| API
    API --> Core["检测、策略、占位符、工作记录"]
    Core --> Protected["平台保护的映射存储"]
    Core --> Result["预览、可导出的副本或回填结果"]
```

| 层 | 要看的实现 | 负责的决定 |
| --- | --- | --- |
| 进程与窗口 | [启动器](../packaging/launcher.py) · [桌面壳](../src/tuomin_gateway/shell/app.py) | 绑定回环端口、选择当前平台后端。无窗口模式也能提供 Web 控制台。 |
| 用户动作 | [桌面桥接](../src/tuomin_gateway/shell/bridge.py) · [剪贴板控制器](../src/tuomin_gateway/shell/clipboard.py) | 区分普通复制回填与就地替换。普通动作写回前再次读取剪贴板，内容已变化就取消。 |
| 本机服务 | [应用启动与授权检查](../src/tuomin_gateway/service/app.py) · [v1 能力接口](../src/tuomin_gateway/service/v1.py) | 管理路由检查令牌与 Host；带应用身份的业务路由检查 capability、作用域与回填合约。 |
| 文档进出 | [文件接口](../src/tuomin_gateway/service/v1_documents.py) · [Word](../src/tuomin_gateway/document/docx_patch.py) · [Excel 与 PowerPoint](../src/tuomin_gateway/document/office_patch.py) | 抽取可处理的文本并保留位置，按格式生成新的脱敏或回填文件；检查不可安全处理的部件。 |
| 平台环境 | [映射存储](../src/tuomin_gateway/store.py) · [平台权限](../src/tuomin_gateway/platform_security.py) · [Windows ACL](../src/tuomin_gateway/windows_acl.py) | Windows 使用当前用户 DPAPI，macOS 使用钥匙串持有密钥进行 AES-GCM 加密。单独运行的演示采用临时测试存储。 |

### Lite 的检测与文档路径

公开桌面发行包使用具名档位 `file_workbench_rules`。绑定该档位的文件任务运行规则和已配置的词典。应用若声明必需 NER，仍需加载模型并满足就绪条件。文件输出还要检查应用绑定、检测器状态、出站扫描等条件；词典命中数量不能替代出站判断。Lite 使用 pdfminer.six 抽取 PDF 的原生文字层；纯图像页面没有可供该路径识别的文字。Word、Excel、PowerPoint 的原格式副本走各自的结构化文本处理路径。[策略定义](../src/tuomin_gateway/profiles.py) · [文件接口](../src/tuomin_gateway/service/v1_documents.py) · [PDF 读取器](../src/tuomin_gateway/document/pdf.py)

### 哪些状态会留下

| 数据 | 产生和保管 | 生命周期 |
| --- | --- | --- |
| 文本预览 | `/redact_snippet` 在内存中生成对照 | 预览接口不会生成可供以后回填的句柄。 |
| 文件任务 | SQLite 记录任务号、文件名、状态与计数；原值映射在独立的受保护存储中 | 回填时需要原任务的映射或有效恢复包。任务记录、导出的文件、映射分别有自己的生命周期。 |
| 映射 | 句柄绑定应用；剪贴板、跨段文本可在命名空间内复用编号 | 底层存储按 TTL 读取/清理。归档命名空间后可在有效期内申请恢复，删除命名空间会回收关联句柄。 |
| 剪贴板状态 | 当前会话文件只保存命名空间、时间、数量等元数据；控制器将成功输出写到平台剪贴板 | 新会话会归档旧会话。默认空闲 8 小时后，下次执行脱敏时切换会话。退出停止本地服务，需另用清除会话、清空剪贴板等操作处理留存数据。 |
| 恢复包与本地导出 | 由用户显式保存。口令加密恢复包内含占位符与原值的关系 | 保管期限由持有人决定。服务端映射过期不会删除用户已经另存的恢复包、原格式副本或其他程序的剪贴板历史。 |

映射、命名空间、工作记录的持久化可以分别追到 [MappingVault](../src/tuomin_gateway/vault.py)、[NamespaceVault](../src/tuomin_gateway/namespace_vault.py)、[任务数据库](../src/tuomin_gateway/jobs.py) 和 [本地会话文件](../src/tuomin_gateway/shell/storage.py)。功能层面的示例见[桌面操作图解](DESKTOP_GUIDE.md)。

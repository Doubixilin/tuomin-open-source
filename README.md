# 脱敏网关

本地敏感信息检测、规则与词典脱敏、占位符映射和严格回填的开源参考实现。

**这是供研究与自行部署的源码版。最小示例不需要外部模型，不调用云端服务，不需要 API Key 或激活码。完整模型检测、PDF 解析、外部应用接入则需要额外准备，不能把最小示例的成功等同于完整系统已就绪。**

## 先跑通一个不依赖模型的示例

需要 Python 3.10 或更新版本。首次安装需要获取普通 Python 依赖；安装完成后，示例运行不联网。

```bash
git clone https://github.com/Doubixilin/tuomin-open-source.git
cd tuomin-open-source
python -m venv .venv
```

macOS / Linux 激活环境：`source .venv/bin/activate`；Windows PowerShell：`.venv\Scripts\Activate.ps1`。

```bash
python -m pip install .
python examples/minimal_demo.py
```

示例只处理代码内明确标注的合成文本，展示“规则＋词典识别 → 占位符脱敏 → 本地回填 → 拒绝未知占位符”。映射只放在进程内存中，不落盘，不读取你的文档或配置，不访问钥匙串，不加载 NER 模型。成功时会输出 `示例验证通过`。

这个例子验证的是**流程和占位符完整性**。它无法据此证明：任意姓名都能被识别、真实合同没有漏检、图片已脱敏、文件内嵌对象已处理，或者发给模型的文本一定安全。规则只能识别覆盖到的格式，词典只能识别已声明的实体。

## 公开版包含什么

| 能力 | 状态和前提 |
| --- | --- |
| 规则检测、词典匹配、重叠合并、脱敏与严格回填 | 最小示例可直接研究，无模型 |
| 会话稳定占位符、命名空间、审计、映射保护、权限控制 | 提供实现与合成测试；部署前按场景配置 |
| 本地 HTTP API 与 Web 控制台 | 安装 `.[serve]` 并准备应用配置 |
| TXT / DOCX / 表格处理 | 提供实现；表格依赖 `.[document]`；旧 DOC 转换依赖 macOS 系统工具 |
| PDF 原生文字提取 | 单独安装 `.[pdf]`，先阅读 PyMuPDF 许可；不提供 OCR |
| 本地中文 NER | 仅提供接入代码；模型权重不随仓库提供，需自行核实许可和准备 |
| MCP、模型代理、外部应用适配 | 提供代码供研究，须自行配置服务地址、Token、上游和应用策略；最小示例不会启用 |
| 桌面安装包、模型全集、自动部署 | 首版不提供 |

## 可选：本地服务

```bash
python -m pip install '.[serve,document]'
```

将 `tuomin_apps.example.json` 复制为 `tuomin_apps.json`，然后：

```bash
tuomin-gateway serve --host 127.0.0.1 --port 8765
```

访问启动日志给出的 `/ui/` 地址。日志会说明管理 Token 文件的位置；不要把 Token 提交到仓库。管理 Token 是访问控制凭据，与 MIT 许可证、可选激活机制不是一回事。社区源码正常运行无需激活。

示例配置中的 `research_demo` 明确关闭 NER，只适合合成流程演示。界面里的其他预设可能要求 NER；未准备模型时应查看 `/readiness` 和降级／阻断提示，不要为了通过检查而关闭真实业务需要的检测器。当前全局 `/readiness` 还会检查未注册请求的 strict 默认策略，因此无模型时返回 503 是预期行为，即使显式配置的无模型合成应用仍能运行。`/healthz` 仅表示进程存活。

macOS 服务映射默认使用钥匙串支持的加密，Windows 使用 DPAPI；Linux 当前存在明文开发存储路径，不能宣称三端都默认加密。离线 CLI 导出的映射也是含原值的明文文件。不要上传映射、原始输入或恢复材料。详见 [安全边界](docs/BOUNDARIES.md)。

## 阅读路线与外部资源

- [核心流程和代码入口](docs/ARCHITECTURE.md)
- [模型与第三方资源索引、准备方法](docs/EXTERNAL_RESOURCES.md)
- [能力范围和安全边界](docs/BOUNDARIES.md)
- [第三方许可说明](THIRD_PARTY_NOTICES.md)
- [贡献与后续更新](CONTRIBUTING.md)
- [版本记录](CHANGELOG.md)

公开样例不包含实际业务材料。回归测试中使用的姓名、号码和组织字符串仅用于构造检测场景，不用于联系或识别现实中的个人；不要用演示词典代替自己的受控业务词典。

## 测试

```bash
python -m pip install '.[dev]'
python -m pytest -q
```

测试使用临时目录和合成数据，不要求下载模型。PDF 测试在未安装 PyMuPDF 时跳过；如接受其许可并需要验证 PDF，可以安装 `.[pdf]` 后重跑。平台专属测试在不适用的平台跳过。首次版本的具体验证范围见 [验证记录](docs/VALIDATION.md)。

## 许可证

本仓库中由作者拥有权利的代码和文档采用 [MIT](LICENSE)。第三方依赖、模型和数据适用各自条款；本项目的 MIT 不会替它们重新授权。尤其 PyMuPDF 的组合使用／分发须另行满足 AGPL 或有效商业授权条件，见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。

你可以研究、修改和复用代码。请不要把它描述为“零泄漏”“所有文件均可安全上传”或已经完成生产安全认证的产品。

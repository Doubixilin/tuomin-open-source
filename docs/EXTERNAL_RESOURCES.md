# 模型与第三方资源索引

核实日期：2026-10-09。此文件用于说明扩展功能需要什么，不授予第三方模型、数据或软件的许可。
仓库不包含权重，不提供第三方资源镜像，不在启动或运行示例时自动下载资源。

## 中文 NER 模型

| 项目 | 内容 |
| --- | --- |
| 模型名称 | `uer/roberta-base-finetuned-cluener2020-chinese` |
| 发布者 | UER |
| 用途 | 辅助识别中文人名、组织、地址等；不能代替规则、词典和人工复核 |
| 官方介绍 | https://huggingface.co/uer/roberta-base-finetuned-cluener2020-chinese |
| 固定版本 | `cddd8fc233e373855a8c0a7f4b7eb83acb686a2b` |
| 官方文件下载入口 | https://huggingface.co/uer/roberta-base-finetuned-cluener2020-chinese/tree/cddd8fc233e373855a8c0a7f4b7eb83acb686a2b |
| 权重许可 | 本次查看的模型卡与文件列表未找到明确的权重再分发许可；尚待核实。训练框架的许可证不能自动视为权重的许可证 |
| 本仓库提供 | 本地加载适配器与纯逻辑测试，不包含权重或训练数据 |

应先阅读上游条款，必要时向发布者核实使用与分发权利。确认适用条件后，自行从上游准备下列文件，放在同一模型目录：

| 文件 | 参考 SHA-256 |
| --- | --- |
| `config.json` | `a80e368cebeaeb01ed195f068b01a65f5b01b87e8059434710a01f3b97a38598` |
| `pytorch_model.bin` | `b865252516115c46bc508167fa2258f198bcce520eb7a1ac4fbf8d50dc361368` |
| `special_tokens_map.json` | `303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3` |
| `tokenizer_config.json` | `21b0a2fba3d74b521cdbd631b2936c2c063c856c3a05533fabee04005c02cd23` |
| `vocab.txt` | `45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c` |

这些哈希用于比较文件一致性，不是许可凭证，也不是安全认证。`.bin` 是 PyTorch 序列化文件，只从可信上游获取，不加载不可信文件。

```bash
python -m pip install '.[ner]'
```

将 `TUOMIN_NER_MODEL_DIR` 设为你准备好的目录。例如 macOS/Linux 使用 `export TUOMIN_NER_MODEL_DIR=/path/to/model`；PowerShell 使用 `$env:TUOMIN_NER_MODEL_DIR = 'C:\path\to\model'`。路径由你自己指定，不必放入仓库。

接入入口：`src/tuomin_gateway/detectors/ner.py`。加载使用 `local_files_only=True`。
需要 NER 的应用应配置 `use_ner=true`；如果缺少 NER 就不能满足业务要求，还应配置 `ner_required=true`。
准备后检查全局 `/readiness` 的模型加载与应用配置结果。全局检查包含未注册请求的 strict 默认策略；无模型时可能返回 503，不能把某个无模型示例成功当成整个服务 ready。要求的模型不可用时应阻断；可选模型不可用时会报告降级，不能据此宣称完整能力就绪。

## 其他模型接入边界

`detectors/openai_privacy_filter.py` 是预留适配接口，**当前没有接通模型运行时**，不能通过放入一个模型目录就宣称已经支持。
研究入口为 [官方项目](https://github.com/openai/privacy-filter) 和 [官方模型页](https://huggingface.co/openai/privacy-filter)。本仓库未打包或再许可其代码、权重和数据；采用前应自行核实版本、条款，并实现和验证适配器。

## 软件依赖

| 资源 | 官方入口 | 对应功能与安装方式 |
| --- | --- | --- |
| cryptography | https://cryptography.io/ | 基础依赖；安装本项目时安装 |
| FastAPI | https://fastapi.tiangolo.com/ | HTTP 服务，`.[serve]` |
| Uvicorn | https://www.uvicorn.org/ | HTTP 服务，`.[serve]` |
| HTTPX | https://www.python-httpx.org/ | HTTP 客户端，`.[serve]` |
| openpyxl | https://openpyxl.readthedocs.io/ | XLSX/XLSM，`.[document]` |
| xlrd | https://xlrd.readthedocs.io/ | XLS，`.[document]` |
| PyMuPDF | https://pymupdf.readthedocs.io/ | PDF 原生文字，单独 `.[pdf]`；先阅读其许可 |
| Transformers | https://huggingface.co/docs/transformers/ | 本地 NER 适配，`.[ner]`；不包含模型权重 |
| PyTorch | https://pytorch.org/ | 本地 NER 运行时，`.[ner]`；CPU/GPU 配置由使用者准备 |

仓库提供 Python 依赖声明，而不是这些软件的副本。实际安装版本应在复现报告中记录。许可说明见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 数据与外部应用

不提供原始业务材料或来源不清的数据集镜像。公开测试以合成数据为主；不提供模型训练数据。
不包含外部应用的源码、凭据或私有配置。外部应用接入只能覆盖实际经过本网关的请求，其他通道需要单独评估。

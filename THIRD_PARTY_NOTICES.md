# 第三方许可说明

本仓库作者拥有权利的代码与文档采用根目录 MIT 许可证。第三方名称用于标识依赖，不表示其作者认可或担保本项目。

源码安装按选择的功能从上游获取依赖。公开桌面构建附带实际收录组件的版本清单与许可文本。Windows 托盘库的对应源码随构建与仓库提供。以下列出主要直接依赖。

| 直接依赖 | 核实的许可类型 | 官方依据 |
| --- | --- | --- |
| cryptography | Apache-2.0 或 BSD-3-Clause | https://cryptography.io/en/latest/faq/#what-license-is-cryptography-under |
| FastAPI | MIT | https://github.com/fastapi/fastapi/blob/master/LICENSE |
| Uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn/blob/main/LICENSE.md |
| HTTPX | BSD-3-Clause | https://github.com/encode/httpx/blob/master/LICENSE.md |
| openpyxl | MIT | https://openpyxl.readthedocs.io/en/stable/ |
| xlrd | BSD | https://github.com/python-excel/xlrd/blob/master/LICENSE |
| Transformers | Apache-2.0 | https://github.com/huggingface/transformers/blob/main/LICENSE |
| PyTorch | BSD 风格主许可及附带组件的其他许可 | https://github.com/pytorch/pytorch/blob/main/LICENSE |
| pdfminer.six | MIT，附带组件有独立声明 | https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE |
| PyMuPDF | GNU AGPL-3.0 或 Artifex 商业授权 | https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright |
| pywebview 5.4 | BSD-3-Clause | https://pypi.org/project/pywebview/5.4/ |
| PyObjC（macOS） | MIT | https://pypi.org/project/pyobjc-core/12.2.2/ |
| pythonnet（Windows） | MIT | https://pypi.org/project/pythonnet/3.0.5/ |
| pystray（Windows） | LGPLv3 | https://pypi.org/project/pystray/0.19.5/ |
| Pillow（Windows） | MIT-CMU | https://pillow.readthedocs.io/en/stable/about.html#license |
| Tomli（Python 3.10 读取 TOML 配置） | MIT | https://github.com/hukkin/tomli/blob/master/LICENSE |
| python-docx（测试文档生成） | MIT | https://github.com/python-openxml/python-docx/blob/master/LICENSE |
| pytest（开发） | MIT | https://github.com/pytest-dev/pytest/blob/main/LICENSE |
| setuptools（构建） | MIT | https://github.com/pypa/setuptools/blob/main/LICENSE |

## PDF 与桌面构建

v0.2.0 公开 Lite 构建通过 pdfminer.six 提取文字型 PDF，附带 CMap 与相关上游声明。源码安装可选 `.[pdf-text]` 使用相同后端。使用另一后端时，`.[pdf]` 会安装 PyMuPDF，其 AGPL 或商业许可适用于相应的组合使用与分发。两种后端都只能读取 PDF 中已有的文字。[PDFMiner 上游仓库](https://github.com/pdfminer/pdfminer.six) · [PyMuPDF 许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)

两个平台使用 CPython 和 PyInstaller。构建内的 `licenses/VERSIONS.json` 列出 Python 组件版本，邻近目录保留许可文本。Windows 的 pystray 以可替换的 Python 源文件运行，发行目录另附对应版本源码、LGPLv3 与 GPLv3 全文，以及 WebView2 SDK 许可和第三方声明。[Windows 组件和替换方法](packaging/windows-public/README.md) · [PyInstaller 许可](https://pyinstaller.org/en/stable/license.html)

## 中文命名实体识别

Lite 使用规则和用户配置的词典。源码中的可选 NER 适配器需要模型框架及本地权重。UER 固定版本权重的再分发授权尚待核实，公开发行物省略权重。源码中已有的其他构建方式可以引入额外库和权重，重新分发时需根据构建内容核对许可。[模型版本与上游入口](docs/EXTERNAL_RESOURCES.md)

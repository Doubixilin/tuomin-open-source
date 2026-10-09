# 第三方许可说明

本仓库作者拥有权利的代码与文档采用根目录 MIT 许可证。第三方名称用于标识依赖，不表示其作者认可或担保本项目。

本仓库不复制第三方库安装目录、不包含模型权重。安装工具从各依赖上游获取软件；这些软件及其传递依赖仍保留自己的许可。下面是直接依赖的索引，不是安装包完整 SBOM，也不是对所有未来版本的许可保证。

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
| PyMuPDF | GNU AGPL-3.0 或 Artifex 商业授权 | https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright |
| pytest（开发） | MIT | https://github.com/pytest-dev/pytest/blob/main/LICENSE |
| setuptools（构建） | MIT | https://github.com/pypa/setuptools/blob/main/LICENSE |

## PDF 的特殊边界

PyMuPDF **不是许可不明的软件**，而是有明确 AGPL／商业许可条件的软件。它不属于最小示例的依赖，也不在默认或 `document` 安装项中；需显式选择 `pdf` 安装项。

本项目 MIT 只授予作者自己的代码权利，不会覆盖 PyMuPDF 的许可。将本项目与 PyMuPDF 组合使用、部署或分发时，必须评估并履行 AGPL 对相应组合程序的义务，或取得适用商业授权。不要把组合安装包宣传为“全部只受 MIT 约束”。将其设为可选依赖本身并不豁免这些义务。

## 模型、数据与二次分发

模型框架许可不自动等于模型权重和训练数据的许可。权重授权状态、官方下载入口及版本见 [资源索引](docs/EXTERNAL_RESOURCES.md)。

如果你制作自己的安装包、镜像或模型合集，应根据实际包含的文件补齐第三方许可文本、版权声明、对应源码等要求；本仓库的索引不替代这些分发义务。

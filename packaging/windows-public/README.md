# Windows 组件与许可

## 系统托盘

Tuomin 使用 pystray 0.19.5 显示系统托盘。完整上游源代码位于同目录的 `pystray/pystray-0.19.5-source.tar.gz`。归档来自作者发布的 v0.19.5 标签，SHA-256 为 `2ab6bd15c14b400c9e8271a82c9de8adc0f1a0cacfc14080bdd4fb42bb88ec92`。LGPLv3 和 GPLv3 全文分别见 `pystray/COPYING.LGPL`、`pystray/COPYING`。[上游源码](https://github.com/moses-palmer/pystray/tree/v0.19.5)

如需运行修改后的兼容版本，请先退出 Tuomin 并备份解压目录，再用归档中 `lib/pystray/` 下对应的 Python 源文件替换 `_internal/pystray/` 下的文件，重新打开 Tuomin。

## 窗口与 WebView2

Windows 桌面窗口使用系统安装的 Microsoft Edge WebView2 Runtime。pywebview 5.4 随附的 Microsoft WebView2 SDK 组件版本为 1.0.2957.106，发行许可和第三方声明见 `WebView2/LICENSE.txt` 与 `WebView2/NOTICE.txt`。[Microsoft SDK 软件包](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.2957.106) · [Microsoft WebView2 Runtime 下载](https://developer.microsoft.com/microsoft-edge/webview2/)

Python 与 .NET 通过 pythonnet 3.0.5 和 clr-loader 0.2.10 通信。两个组件的许可全文分别取自对应版本的官方源码发行包，位于 `pythonnet/LICENSE.txt` 和 `clr-loader/LICENSE.txt`。[pythonnet 发行文件](https://pypi.org/project/pythonnet/3.0.5/#files) · [clr-loader 发行文件](https://pypi.org/project/clr-loader/0.2.10/#files)

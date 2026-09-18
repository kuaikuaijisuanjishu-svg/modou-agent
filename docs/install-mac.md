# macOS 安装

## 要求

- macOS 12 或更新版本；Apple silicon 与 Intel 均可。
- 系统中有 Python 3.10 或更新版本的 `python3`。
- 首次安装需要网络下载依赖；确定性演示运行时不需要模型 API。

## 安装和启动

1. 解压 `shuimu-yanma-v0.2-public-mac-*.zip` 到任意目录（支持中文和空格路径）。
2. 双击 **安装水木验码.command**。它会在包内建立虚拟环境、安装锁定依赖、生成演示仓库并执行完整性检查。
3. 双击 **启动水木验码.command**，回车选择确定性模式，然后在浏览器打开终端打印的本地地址。
4. 演示结束后双击 **停止水木验码.command**；需要恢复演示数据时双击 **重置演示.command**。

遇到问题可双击 **诊断水木验码.command**，再提交脱敏后的报告。包内 VSIX 可用 VS Code 的“Install from VSIX”安装；MCP 连接方式见 [接入说明](integrations.md)。

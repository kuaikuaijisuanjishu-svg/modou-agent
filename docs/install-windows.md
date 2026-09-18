# Windows + WSL2 安装

Windows 包的后端运行在 WSL2 的 Linux 文件系统中，浏览器和编辑器入口在 Windows 侧使用；它不是原生 Windows 后端。目标机器未完成实测前，该资产只标为未验证预览。

## 要求

- Windows 10 2004（build 19041）或 Windows 11。
- 已安装 WSL2 和至少一个发行版（例如 Ubuntu 22.04）；安装或升级 WSL 可能需要管理员权限和重启。
- 建议至少 8 GB 内存；WSL 内约需 1.5 GB 磁盘。
- 首次安装需要网络；确定性演示不需要模型 API。

## 安装和启动

1. 将 `shuimu-yanma-v0.2-public-win10-wsl2-*.zip` 解压到任意目录。
2. 在 PowerShell 中运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   powershell -ExecutionPolicy Bypass -File start.ps1
   ```

3. 终端会打印带一次性令牌的本地地址；在浏览器打开它并进入评委模式。
4. 演示结束运行 `stop.ps1`；需要恢复演示数据时运行 `reset.ps1`。

缺少 WSL2 时，请先按微软官方文档完成安装，再重新运行 `install.ps1`。不要把 Windows 包当作无需 WSL 的原生版本宣传。

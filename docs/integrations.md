# CLI、MCP 与 VS Code 接入

所有入口都连接同一本地后台，读取同一任务和版本回执。用户负责选择仓库、批准计划和创建授权；入口不会自行扩大权限。

在已经启动的本地后台上使用包内 Python 解释器运行客户端。连接地址和一次性令牌只从当前会话或本地运行目录读取，不要把令牌写入仓库或提交到日志：

```bash
python -m modou.mcp --connect http://127.0.0.1:8765 --token-file ../run/server.token
```

实际端口以启动器输出为准。使用 VS Code 时设置 `shuimu.projectPath`、`shuimu.pythonPath` 和 `shuimu.server`，首次连接时输入本次服务令牌。扩展只支持受信本地工作区，不发布到扩展市场。

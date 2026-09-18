# 水木验码 VS Code 本地入口（实验版）

扩展通过本地 Python 客户端连接已经运行的水木验码后台，提供“验收当前改动”“打开任务”“查看回执”和“检查受托任务”四个入口。它不会启动第二个审查管理器、批准计划或自动提交代码。

## 使用

1. 启动本地后台并登记目标仓库。
2. 在 VS Code 选择“Extensions: Install from VSIX”安装随包提供的 VSIX。
3. 在设置中填写 `shuimu.projectPath`、`shuimu.pythonPath` 和 `shuimu.server`。
4. 首次连接时输入启动令牌。令牌保存在 VS Code SecretStorage，不写入 `settings.json`。
5. 选择已登记仓库，描述验收目标，然后在本地工作台确认计划。

扩展只支持受信本地工作区，不发布到市场，也不承诺远程开发环境。查看回执时以服务器返回的状态和未完成项为准，不从绿灯或执行结束推导任务成功。

## 本地检查与打包

```bash
node --test extensions/shuimu-evidence/extension.test.js
python3 extensions/shuimu-evidence/package_vsix.py /tmp/shuimu-evidence.vsix
```

VSIX 不包含测试代码、项目密钥或私人路径。宿主内命令点击仍需在你自己的 VS Code 会话中确认。

# 墨斗 Modou

> 用测试证据检查 AI 补丁是否真正被代码库承载。

这是给评委运行的最小展示包，包含一个可重复的本地演示和可选的本地 Review Cockpit。展示包不包含内部评测数据、历史运行产物、实验记录、模型原始响应或开发环境信息。

## 快速运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/run_demo.py
```

演示会在临时 fixture 仓库中运行一次审查，展示新增代码、测试结果和可回放的公开证据摘要。运行产生的文件保存在本机运行目录，不会写回展示仓库。

## 可选界面

```bash
npm --prefix web install
npm --prefix web run build
python demo/run_demo.py
python -m modou.server --allow-repo demo/retry_demo
```

服务只监听本机回环地址，并要求启动令牌。评委可用浏览器打开终端打印的本地地址体验界面。

## 公开边界

- 只接受评委明确指定的本地仓库和仓库内测试路径。
- 结论只针对实际运行过的测试范围，不把未覆盖部分推断为安全。
- 系统不会自动提交、推送或修改评委仓库的历史。
- 对外证据包会移除绝对路径、主机身份、源码行、原始命令输出、原始模型响应和内部标识。
- 本包没有 GitHub App、评测数据集、历史运行目录或内部验收脚本。

更具体的本地安全边界见 `docs/security-boundary.md`。发布前检查使用：

```bash
python tools/public_release_check.py
```

## 目录

| 路径 | 用途 |
| --- | --- |
| `modou/` | 运行时审查引擎和本地服务 |
| `demo/` | 自足的最小演示 fixture |
| `web/` | 可选的本地 Review Cockpit |
| `configs/review-presets.example.json` | 一个脱敏的演示 preset |
| `tools/public_release_check.py` | 发布前公开边界检查 |

## 许可

本展示包暂未指定开源许可证；如需复用代码，请先联系项目维护者。

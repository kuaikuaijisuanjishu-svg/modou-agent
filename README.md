# 水木验码 Shuimu Yanma

> 用可逆实验验证新增代码是否真正受到测试约束。
> Verify whether newly added code is genuinely constrained by tests through reversible experiments.

[![CI](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/kuaikuaijisuanjishu-svg/modou-agent?display_name=tag)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-6C5CE7)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

当前公开版本：**v0.1.0**（评审展示版）

“水木”取自“水木清华”的文化意象，“验码”对应对代码和测试证据的验证。本项目是独立参赛作品，不代表清华大学官方产品或官方背书。

## 它解决什么问题

AI 写出的补丁即使测试全绿，也不代表每一行新增代码都被测试真正约束。水木验码会在隔离工作区中临时移除新增代码，重新运行仓库原有测试，观察具体哪条测试发生变化，然后恢复现场。

```text
本地 Git 仓库 + 明确的测试范围
              ↓
          冻结检查计划
              ↓
     在隔离工作区运行可逆实验
              ↓
      测试结果 + 恢复状态校验
              ↓
       三态结论与可回放证据包
```

- **承重**：临时移除候选代码后，出现具名测试失败。
- **无据**：声明的测试范围没有为该候选提供足够约束。
- **游离**：新增文件未进入已观察到的测试或引用路径。
- **边界纪律**：结论只适用于实际运行的测试范围，不等于证明代码正确、可安全删除或语义等价。

## 5 分钟运行公开演示

```bash
git clone https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
cd modou-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
```

启动本地界面：

```bash
npm --prefix web ci
npm --prefix web run build
python -m modou.server \
  --allow-repo demo/retry_demo \
  --preset-config configs/review-presets.example.json
```

服务只监听本机回环地址，并要求一次性启动令牌。请在浏览器打开终端打印的完整本地地址。

## 验证公开包

```bash
python tools/public_release_check.py
python tests/run.py
npm --prefix web test
npm --prefix web run build
npm --prefix web run test:e2e
```

以上命令也是 GitHub Actions 的发布门禁；端到端检查使用公开 fixture，并验证人工批准、真实服务链路和仓库路径边界。

## 公开版与完整研究版的边界

这个仓库是经过脱敏处理的**公开展示包**，按照公开项目的方式维护，但不是完整研究工作区。

公开仓库包含：

- 可运行的核心审查闭环、最小演示和本地界面；
- 公开安全边界、版本记录、贡献与安全反馈方式；
- 为验证公开内容而设置的最小测试和发布检查。

公开仓库不包含，也不会通过 README、提交历史、Issue、Release 或构建产物披露：

- 历史测试、内部验收规则、未公开评测数据和原始运行记录；
- 项目历史计划、冲刺看板、内部研究笔记和模型原始响应；
- 个人绝对路径、主机信息、密钥、账户标识或私有仓库位置；
- 尚未通过公开门槛的能力主张和完整私有实现。

**位置说明：**公开展示版以本 GitHub 仓库为唯一公开位置；完整研究版保存在独立的非公开工作区。公开文档只描述两者的边界，不公布私有工作区的本地路径、目录结构或历史内容。

发布规则：

1. 只从事先建立的公开白名单中选择文件，不从完整研究版整体复制。
2. 每次发布先运行敏感信息与目录边界检查，再运行最小测试和前端构建。
3. 新功能只有在公开代码、公开说明和公开验证同时具备时才进入 Release。
4. 公开证据包移除绝对路径、源码正文、原始命令输出、原始模型响应和内部标识。
5. Apache-2.0 仅适用于本仓库实际发布的文件；未进入本仓库的私有材料不因本许可证而获得授权。

更具体的边界见 [docs/security-boundary.md](docs/security-boundary.md)，架构和资源边界见 [docs/architecture-overview.md](docs/architecture-overview.md)，漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 目录

| 路径 | 公开用途 |
| --- | --- |
| `modou/` | 内部兼容包名：状态、策略、证据引擎和本地服务 |
| `demo/` | 可重建的最小演示仓库 |
| `web/` | 水木验码本地 Review Cockpit |
| `configs/` | 脱敏的能力状态和演示预设 |
| `tests/` | 公开边界的最小回归测试 |
| `tools/public_release_check.py` | 发布前隐私与结构检查 |

## 项目维护

- 版本变化：[CHANGELOG.md](CHANGELOG.md)
- 参与方式：[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 安全反馈：[SECURITY.md](SECURITY.md)
- 发布页面：[GitHub Releases](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)

## 许可与标识

本仓库代码按 [Apache License 2.0](LICENSE) 许可，详见 [NOTICE](NOTICE)。

“水木验码 / Shuimu Yanma”名称和图形标识不因代码许可证而自动获得商标或品牌使用授权。内部 `modou/` 包名仅用于兼容导入。详见 NOTICE。

---

## English

**Shuimu Yanma v0.1.0** is a sanitized, runnable public showcase. It uses reversible experiments to test whether newly added code is genuinely constrained by named tests, then verifies workspace restoration and emits replayable evidence.

The public repository contains the core review loop, a rebuildable demo, the local UI, and minimal release checks. It intentionally excludes historical tests, internal rules, evaluation data, private plans, raw runs, model transcripts, credentials, host identity, and the location of the private research workspace.

The Apache-2.0 license applies only to files actually published in this repository. Unpublished private materials are outside this license. This is an independent competition project and is not an official Tsinghua University product or endorsement.

# 快速上手

在公开源码树根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
(cd web && npm ci)
(cd web && npm run build)
python -m modou.server --allow-repo demo/retry_demo --preset-config configs/review-presets.example.json
```

打开终端打印的本地地址。先选择项目和验收要求，再查看计划、确认计划并阅读回执。标准确定性路径不调用模型。

公开检查：`python tools/public_release_check.py --root . --notes-file docs/release/v0.2.0-experimental.1.md`、`python tests/run.py`、`(cd web && npm test)` 和 `(cd web && npm run build)`。

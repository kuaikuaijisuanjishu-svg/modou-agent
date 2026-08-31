"""账本落盘：JSONL，带版本与完成标志。

为什么必须有完成标志：进程被打断会留下一份**语法完全合法**的 JSONL，
从文件本身看不出它是完整的还是半份的。所以最后一条必须是 `LedgerComplete`，
读的时候没读到就判残缺——和 `run_status.json` 停在 INCOMPLETE 是同一条纪律。

写入是原子的：先写 `.partial` 再 rename。半份账本比没有账本更坏。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..runroot import write_atomic
from . import validate
from .records import LEDGER_COMPLETE, Record, ledger_complete

FILENAME = "ledger.jsonl"


class LedgerIncomplete(RuntimeError):
    """账本没有收尾标记。这次运行的账本不可采信。"""


def serialize(records: list[Record], run_id: str) -> str:
    counts: dict[str, int] = {}
    for r in records:
        counts[r.record_type] = counts.get(r.record_type, 0) + 1
    lines = [json.dumps(r.to_json(), ensure_ascii=False) for r in records]
    lines.append(json.dumps(ledger_complete(run_id, counts).to_json(),
                            ensure_ascii=False))
    return "\n".join(lines) + "\n"


def write(dest: Path, records: list[Record], run_id: str) -> Path:
    return write_atomic(dest, serialize(records, run_id))


def read(path: Path, *, strict: bool = True) -> list[dict]:
    """读回账本并**全面校验**。

    不只看收尾标记：schema 版本、record_id 是否真的内容寻址、run_id 是否一致、
    收尾计数对不对、主张的 provenance 是否真的落地——任一不过就抛。
    读的时候不查，等于把「账本自洽」寄托在写的时候没出错，那不叫校验。
    """
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if not rows or rows[-1]["record_type"] != LEDGER_COMPLETE:
        raise LedgerIncomplete(
            f"{path} 没有 {LEDGER_COMPLETE} 收尾标记，账本可能是被中断的半份")
    if not strict:
        return rows[:-1]
    problems = validate.check_complete(rows) + validate.validate(rows[:-1])
    validate.raise_if_bad(problems, what=f"{path.name} ")
    return rows[:-1]


def of_type(rows: list[dict], record_type: str) -> list[dict]:
    return [r for r in rows if r["record_type"] == record_type]

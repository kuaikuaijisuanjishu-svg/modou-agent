"""Capability maturity: what the evidence says, and what may be said in public.

Every capability in this product sits in exactly one of four states, and the
state is decided by evidence that was gated *before* it was measured:

    verified         cleared its own frozen threshold on real data
    experimental     implemented and honest, but its threshold is unmet
    negative_result  measured, and the measurement did not support the claim
    disabled         deliberately not available, or withdrawn after review

The states exist to be *enforced*, not merely declared.  A registry that only
described states would drift the moment somebody wrote a slide.  So the same
file that records a state also records:

  * `mentions` — how this capability is named in public material;
  * `disclosures` — the qualification a non-verified capability must carry
    wherever it is mentioned;
  * `forbidden` — the specific sentences its state does not entitle anyone to
    write, each with the reason.

`check_documents` reads public material and fails closed on both: a
non-verified capability mentioned with no disclosure nearby, and any forbidden
claim.  The release pipeline runs it over the published package, so a README, a
fact sheet or a narration script cannot quietly outrun the evidence.

The runtime axis is separate on purpose.  "The evidence did not support this"
and "you may not turn this on" are different statements: model scheduling lost
its comparison and remains selectable, while the Evidence Auditor missed its
threshold and is switched off.  Only `disabled` forces `unavailable`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


SCHEMA_VERSION = "modou-capability-registry-v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "configs" / "capabilities.json"
DOCUMENT_SUFFIXES = frozenset({".md", ".txt", ".html"})


class CapabilityError(ValueError):
    """The registry itself is malformed. Never silently degraded."""


class State(str, Enum):
    VERIFIED = "verified"
    EXPERIMENTAL = "experimental"
    NEGATIVE_RESULT = "negative_result"
    DISABLED = "disabled"


class Runtime(str, Enum):
    DEFAULT = "default"
    OPT_IN = "opt_in"
    UNAVAILABLE = "unavailable"


#: States whose every public mention must carry a qualification.  `verified` is
#: the only state that may stand on its own.
NEEDS_DISCLOSURE = frozenset({State.EXPERIMENTAL, State.NEGATIVE_RESULT,
                              State.DISABLED})

STATE_LABELS = {
    State.VERIFIED: "已验证",
    State.EXPERIMENTAL: "实验性",
    State.NEGATIVE_RESULT: "负结果",
    State.DISABLED: "已关闭",
}


@dataclass(frozen=True)
class Violation:
    path: str
    capability_id: str
    code: str
    detail: str

    def to_json(self) -> dict:
        return {"path": self.path, "capability_id": self.capability_id,
                "code": self.code, "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.path}: [{self.capability_id}] {self.code} — {self.detail}"


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    state: State
    runtime: Runtime
    summary: str
    gate: str
    evidence: tuple[str, ...]
    mentions: tuple[re.Pattern, ...]
    disclosures: tuple[re.Pattern, ...]
    forbidden: tuple[tuple[re.Pattern, str], ...]

    @property
    def needs_disclosure(self) -> bool:
        return self.state in NEEDS_DISCLOSURE

    def mentioned_in(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.mentions)

    def disclosed_in(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.disclosures)

    def public_json(self) -> dict:
        """The browser boundary: states and prose only, never the patterns.

        The regexes are an enforcement detail. Shipping them would invite
        writing around them, which is exactly the failure this guards against.
        """
        return {
            "id": self.id, "title": self.title, "state": self.state.value,
            "state_label": STATE_LABELS[self.state],
            "runtime": self.runtime.value, "summary": self.summary,
            "gate": self.gate, "evidence": list(self.evidence),
        }


def _patterns(raw: object, *, field: str, capability: str) -> tuple[re.Pattern, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise CapabilityError(f"{capability}.{field} must be non-empty strings")
    out = []
    for item in raw:
        try:
            out.append(re.compile(item, re.IGNORECASE))
        except re.error as exc:
            raise CapabilityError(f"{capability}.{field} has an invalid regex: {exc}") from exc
    return tuple(out)


def _capability(raw: object) -> Capability:
    if not isinstance(raw, dict):
        raise CapabilityError("each capability must be an object")
    required = {"id", "title", "state", "runtime", "summary", "gate",
                "evidence", "mentions", "disclosures", "forbidden"}
    if set(raw) != required:
        missing = sorted(required - set(raw)) or sorted(set(raw) - required)
        raise CapabilityError(f"capability fields do not match the schema: {missing}")
    identifier = str(raw["id"])
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        raise CapabilityError(f"capability id is not a stable slug: {identifier!r}")
    try:
        state = State(str(raw["state"]))
        runtime = Runtime(str(raw["runtime"]))
    except ValueError as exc:
        raise CapabilityError(f"{identifier}: {exc}") from exc
    evidence = raw["evidence"]
    if not isinstance(evidence, list) or not all(isinstance(x, str) and x for x in evidence):
        raise CapabilityError(f"{identifier}.evidence must be repository-relative paths")
    for item in evidence:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise CapabilityError(f"{identifier}.evidence path escapes the repository: {item}")
    forbidden_raw = raw["forbidden"]
    if not isinstance(forbidden_raw, list):
        raise CapabilityError(f"{identifier}.forbidden must be a list")
    forbidden = []
    for entry in forbidden_raw:
        if (not isinstance(entry, dict) or set(entry) != {"pattern", "why"}
                or not isinstance(entry["why"], str) or not entry["why"]):
            # The reason is mandatory: a bare banned phrase teaches the next
            # writer nothing, and the checker's message is the whole product.
            raise CapabilityError(f"{identifier}.forbidden entries need pattern and why")
        forbidden.append((_patterns([entry["pattern"]], field="forbidden",
                                    capability=identifier)[0], entry["why"]))
    capability = Capability(
        id=identifier, title=str(raw["title"]), state=state, runtime=runtime,
        summary=str(raw["summary"]), gate=str(raw["gate"]),
        evidence=tuple(evidence),
        mentions=_patterns(raw["mentions"], field="mentions", capability=identifier),
        disclosures=tuple(_patterns(raw["disclosures"], field="disclosures",
                                    capability=identifier))
        if raw["disclosures"] else (),
        forbidden=tuple(forbidden))
    if state is State.DISABLED and runtime is not Runtime.UNAVAILABLE:
        raise CapabilityError(
            f"{identifier}: a disabled capability must not stay runtime-selectable")
    if state is State.VERIFIED and not evidence:
        raise CapabilityError(f"{identifier}: 'verified' requires named evidence")
    if capability.needs_disclosure and not capability.disclosures:
        raise CapabilityError(
            f"{identifier}: state {state.value} requires at least one disclosure")
    return capability


def load(path: Path | str | None = None) -> tuple[Capability, ...]:
    """Load and fully validate the registry, or raise. There is no partial load."""
    source = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityError(f"capability registry unreadable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise CapabilityError("capability registry schema is unsupported")
    items = raw.get("capabilities")
    if not isinstance(items, list) or not items:
        raise CapabilityError("capability registry is empty")
    capabilities = tuple(_capability(item) for item in items)
    ids = [x.id for x in capabilities]
    if len(ids) != len(set(ids)):
        raise CapabilityError("capability ids must be unique")
    return capabilities


def by_id(capabilities: tuple[Capability, ...]) -> dict[str, Capability]:
    return {x.id: x for x in capabilities}


def runtime_allowed(capability_id: str, *,
                    capabilities: tuple[Capability, ...] | None = None) -> bool:
    """Whether the product may switch this capability on at all."""
    registry = by_id(capabilities if capabilities is not None else load())
    capability = registry.get(capability_id)
    if capability is None:
        raise CapabilityError(f"unknown capability: {capability_id}")
    return capability.runtime is not Runtime.UNAVAILABLE


def missing_evidence(capabilities: tuple[Capability, ...], *,
                     repo: Path) -> list[Violation]:
    """A `verified` state that points at a file which is not there is a lie."""
    out = []
    for capability in capabilities:
        for item in capability.evidence:
            if not (repo / item).is_file():
                out.append(Violation("configs/capabilities.json", capability.id,
                                     "evidence_missing", item))
    return out


#: Words that turn the sentence around them into a prohibition rather than a
#: claim.  Deliberately narrow — a marker only counts when it governs the match
#: directly (same line before it, or the heading/lead-in of its block).
_DENIAL_MARKERS = re.compile(
    r"不得|不可|禁止|禁用|严禁|不能|不许|不允许|不应|不说|不宣传|不主张|不写|"
    r"避免|不要|杜绝|不做|不增加|不给出|不提供|不支持|不会|一律不|撤下|"
    r"不竞争|非目标|延期|暂不|范围外|排除|不承诺|待完成|尚未|拒绝|不采用|反例|"
    # 纠正错误说法的句子本身不是被纠正的主张。
    r"不是|而非|并非|已被取代|旧口径|"
    r"must not|never|do not|don't|forbidden|prohibited|out of scope|not ")
#: A claim quoted in order to be *named as* a misunderstanding is not a claim.
#: Checked across the whole line, not just after the match: "X 会被读成 Y" puts
#: the marker before the phrase, "『Y』的误解为 0" puts it after, and both are
#: the same act — talking about the wrong reading rather than committing it.
_MISCONCEPTION = re.compile(
    r"误解|误读|误认为|误以为|谣传|不实|以为|读成|当成|当作|误当")
#: A negation particle sitting directly on the phrase ("不证明语义等价").
_NEGATION_PARTICLES = "不无非未没否"
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_LEAD_IN = re.compile(r"[：:]\s*$")
#: A wrapped sentence still governs its own tail. "不得使用 94.0%、80.6%、\n
#: 「71%」" put the prohibition on the previous line and the figure on this one,
#: which the line-scoped check read as a bare claim.
_SENTENCE_END = re.compile(r"[。！？；.!?;:：]\s*$")
#: ...but only for real continuations. A new list item, table row or heading
#: starts its own sentence, so a prohibition above must not bleed into it —
#: that would turn neighbouring table rows into blanket exemptions.
_STARTS_NEW_BLOCK = re.compile(r"^\s{0,3}(?:[-*+>|]|#{1,6}\s|\d+[.)]\s)")
#: How far back a governing heading may sit. Beyond this the match stands alone.
_GOVERNING_LOOKBACK = 30


def denial_context(text: str, index: int) -> bool:
    """Is this occurrence a prohibition of the claim rather than the claim?

    The house style lists banned wording explicitly ("禁用：安全删除、语义等价"),
    so a checker that only matched substrings would flag every style guide that
    does its job — and the fastest way to silence it would be to delete the
    guide. Three contexts count as a denial, and only three:

      1. a negation particle sitting directly on the phrase, or a prohibition
         marker earlier on the same line;
      2. the phrase being named as a misunderstanding later on the same line
         ("『会自动删除代码』的误解为 0");
      3. the nearest preceding heading or lead-in line that governs the block
         ("### 非目标", "### 禁用", "以下说法一律不得使用：").

    Anything further away does not excuse the sentence.

    Shared with `modou.facts`: the banned-number discipline hits exactly the
    same trap — the document that lists the forbidden figures is the one most
    worth keeping.
    """
    if index and text[index - 1] in _NEGATION_PARTICLES:
        return True
    line_start = text.rfind("\n", 0, index) + 1
    if _DENIAL_MARKERS.search(text[line_start:index]):
        return True
    line_end = text.find("\n", index)
    line_end = len(text) if line_end < 0 else line_end
    if _MISCONCEPTION.search(text[line_start:line_end]):
        return True
    if line_start and not _STARTS_NEW_BLOCK.match(text[line_start:line_end]):
        previous_end = line_start - 1
        previous_start = text.rfind("\n", 0, previous_end) + 1
        previous = text[previous_start:previous_end]
        if (previous.strip() and not _SENTENCE_END.search(previous)
                and _DENIAL_MARKERS.search(previous)):
            return True
    before = text[:line_start].splitlines()
    for line in reversed(before[-_GOVERNING_LOOKBACK:]):
        if _HEADING.match(line) or _LEAD_IN.search(line):
            # The nearest governing line decides; a further one does not.
            return bool(_DENIAL_MARKERS.search(line))
    return False


def _asserted(pattern: re.Pattern, text: str) -> re.Match | None:
    """The first occurrence that is actually being asserted, if any."""
    for match in pattern.finditer(text):
        if not denial_context(text, match.start()):
            return match
    return None


def check_text(text: str, *, path: str,
               capabilities: tuple[Capability, ...]) -> list[Violation]:
    """Check one document against every capability's state."""
    out: list[Violation] = []
    for capability in capabilities:
        for pattern, why in capability.forbidden:
            match = _asserted(pattern, text)
            if match:
                out.append(Violation(
                    path, capability.id, "forbidden_claim",
                    f"{match.group(0).strip()[:80]!r} — {why}"))
        if not capability.needs_disclosure:
            continue
        # A document that only ever mentions the capability to rule it out has
        # already qualified it; requiring a second disclosure would be noise.
        asserted = any(_asserted(pattern, text) for pattern in capability.mentions)
        if asserted and not capability.disclosed_in(text):
            out.append(Violation(
                path, capability.id, "missing_disclosure",
                f"状态为“{STATE_LABELS[capability.state]}”，提到它的材料必须同时写明其边界"))
    return out


def check_documents(root: Path, *, capabilities: tuple[Capability, ...],
                    paths: list[Path] | None = None) -> list[Violation]:
    """Check every public document under `root` (or an explicit file list)."""
    files = sorted(paths) if paths is not None else sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES)
    out: list[Violation] = []
    for file in files:
        if file.suffix.lower() not in DOCUMENT_SUFFIXES:
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        try:
            name = file.relative_to(root).as_posix()
        except ValueError:
            name = file.name
        out.extend(check_text(text, path=name, capabilities=capabilities))
    return out


PUBLIC_MATERIAL_PATH = (Path(__file__).resolve().parents[1] / "configs"
                        / "public_material.json")


def outward_documents(config: Path | None = None) -> tuple[Path, list[Path]]:
    """The declared set of outward-facing material, and where it is rooted.

    Both checkers need to agree on what "对外材料" means, and leaving it to
    whatever paths someone types is the same reliance on memory these checkers
    exist to remove. The archives are excluded *by name and with a reason* in
    the config: historical evidence legitimately contains superseded figures,
    and editing that out would forge the audit trail rather than fix anything.
    """
    source = Path(config) if config is not None else PUBLIC_MATERIAL_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityError(f"public material list unreadable: {exc}") from exc
    if raw.get("schema_version") != "modou-public-material-v1":
        raise CapabilityError("public material schema is unsupported")
    root = (source.parent.parent / str(raw.get("root") or ".")).resolve()
    entries = raw.get("paths")
    if not isinstance(entries, list) or not entries:
        raise CapabilityError("public material list is empty")
    files: list[Path] = []
    for entry in entries:
        target = root / str(entry)
        if not target.exists():
            raise CapabilityError(f"declared public material is missing: {entry}")
        if target.is_dir():
            files.extend(p for p in sorted(target.rglob("*"))
                         if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES
                         and "archive" not in p.relative_to(target).parts)
        elif target.suffix.lower() in DOCUMENT_SUFFIXES:
            files.append(target)
    return root, sorted(set(files))


def public_registry(capabilities: tuple[Capability, ...] | None = None) -> dict:
    """The payload the cockpit renders, and the one the video must agree with."""
    items = capabilities if capabilities is not None else load()
    counts: dict[str, int] = {}
    for capability in items:
        counts[capability.state.value] = counts.get(capability.state.value, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "capabilities": [x.public_json() for x in items],
        "counts": dict(sorted(counts.items())),
        "scope_note": ("状态由预先冻结的门槛判定，不因为演示需要而改动；"
                       "未达门槛的能力保留为负结果或关闭，不改写成通过。"),
    }

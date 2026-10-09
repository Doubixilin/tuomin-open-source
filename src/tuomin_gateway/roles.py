"""Transaction-role assignment for role-aware placeholders (角色化占位).

For relationship analysis (义务传导 / 协议包穿透) the cloud model must see WHO
plays WHICH role — 甲方 / 乙方 / 总包 / 分包 / 担保人 — not just that "an org"
appears. So instead of masking every party to a flat ``<ORG_001>``, we relabel an
ORG/SUPPLIER entity introduced by a role cue to a role-typed placeholder
(``<PARTYA_001>`` …), preserving the role graph while still stripping identity.

Engineering constraint (strategy doc §5.4): role prefixes are ENGLISH UPPERCASE
so the placeholder stays compatible with the canonical grammar
(``<[A-Z][A-Z0-9_]*_\\d{3,}>``) and the refill integrity check — Chinese role
names live only here / in audit, never in the placeholder.

This runs after fusion + profile filtering and before placeholder generation
(see ``session.collect_detections``). It only *tags* spans (``metadata["role"]``);
the redactor/session decide the final prefix. Role values per scenario are the
user's法务 decision — first set is "contract"; cues/roles are tuned per stage.
"""
from __future__ import annotations

import re
from dataclasses import replace

from tuomin_gateway.schemas import DetectionSpan

# Only entity types that can carry a transaction role.
ROLE_ELIGIBLE_LABELS = {"ORG", "SUPPLIER"}

# How many characters to the LEFT of an entity to scan for a role cue. The
# nearest (right-most) cue in this window wins.
_CUE_WINDOW = 10

# scenario name -> ordered (cue pattern, english role prefix). 中文角色名仅注释。
ROLE_SETS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "contract": [
        (re.compile(r"甲方|发包人|发包方"), "PARTYA"),       # 甲方/发包人
        (re.compile(r"乙方|承包人|承包方"), "PARTYB"),       # 乙方/承包人
        (re.compile(r"总包|总承包"), "GENCON"),              # 总包
        (re.compile(r"分包"), "SUBCON"),                    # 分包
        (re.compile(r"担保人|保证人"), "GUARANTOR"),         # 担保人
        (re.compile(r"监理"), "SUPERVISOR"),                # 监理
        (re.compile(r"委托方|委托人"), "PRINCIPAL"),         # 委托方
    ],
    # 投前投资项目：保留投资结构的角色关系（牵头/合作/标的/投资方）。
    "investment": [
        (re.compile(r"牵头方|牵头投资|主投"), "LEAD"),           # 牵头投资方
        (re.compile(r"标的方|标的公司|投资标的|被投方|被投"), "TARGET"),  # 标的/被投方
        (re.compile(r"合作方|合作伙伴|联合体"), "PARTNER"),      # 合作方
        (re.compile(r"投资方|投资人"), "INVESTOR"),             # 投资方
        (re.compile(r"转让方|出让方"), "TRANSFEROR"),           # 转让方
        (re.compile(r"受让方"), "TRANSFEREE"),                 # 受让方
    ],
}


def _nearest_cue(window: str, role_set: list[tuple[re.Pattern[str], str]]) -> str | None:
    """Return the role prefix of the cue closest to the entity (right-most match
    in the left-context window), or None."""
    best_pos, best_role = -1, None
    for pattern, prefix in role_set:
        for match in pattern.finditer(window):
            if match.end() > best_pos:
                best_pos, best_role = match.end(), prefix
    return best_role


def assign_roles(
    text: str, spans: list[DetectionSpan], role_set_name: str
) -> list[DetectionSpan]:
    """Tag role-eligible spans with ``metadata["role"]`` based on a nearby cue.

    Two passes so the assignment is CONSISTENT across mentions: a value that
    carries a role cue at ANY mention applies that role to ALL its mentions
    (otherwise the same party could become ``<PARTYA_001>`` once and
    ``<ORG_002>`` elsewhere, breaking the relationship graph). Returns new spans;
    non-eligible spans and the no-cue case are returned unchanged.
    """
    role_set = ROLE_SETS.get(role_set_name)
    if not role_set:
        return spans

    value_role: dict[str, str] = {}
    for span in spans:
        if span.label not in ROLE_ELIGIBLE_LABELS:
            continue
        window = text[max(0, span.start - _CUE_WINDOW) : span.start]
        role = _nearest_cue(window, role_set)
        if role:
            value = span.metadata.get("canonical_value", text[span.start : span.end])
            value_role[value] = role  # last cue wins on conflict (rare)

    if not value_role:
        return spans

    tagged: list[DetectionSpan] = []
    for span in spans:
        value = span.metadata.get("canonical_value", text[span.start : span.end])
        role = value_role.get(value)
        if span.label in ROLE_ELIGIBLE_LABELS and role:
            tagged.append(replace(span, metadata={**span.metadata, "role": role}))
        else:
            tagged.append(span)
    return tagged

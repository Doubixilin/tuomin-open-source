"""Half-width translation view for detection scanning (full-width closure).

Chinese business text routinely mixes full-width ASCII variants (０-９, Ａ-Ｚ,
ａ-ｚ) with half-width forms — scanner output, spreadsheet exports, messenger
copy-paste. Rules and dictionary needles written for half-width characters
silently miss the full-width forms entirely (a real leak path:
``电话１３９００００１１１１`` matched nothing before this module existed).

Every mapping here is exactly 1:1, so the translated view has the SAME length
as the original and every offset is valid in both — no index-mapping table is
needed. Detectors scan the translated view but ALWAYS slice spans (and hence
canonical values / mapping originals) from the ORIGINAL text, preserving the
``text[start:end] == canonical_value`` invariant and the exact-surface refill
contract. Value-PARSING logic (amounts, dates, ids) consumes the half-width
view so full-width digits parse correctly.

What is translated — and what is deliberately NOT:

- Translated: full-width digits/letters, and the punctuation that lives
  INSIDE values (－．／：％＠＿＝￥ + ideographic space). Cue patterns
  (``[:：]`` etc.) already accept both widths, so cue semantics are unchanged.
- NOT translated: boundary punctuation (，。！？；、（）【】《》 quotes).
  Lookaheads and stop-char sets in the patterns reference the full-width forms
  (e.g. the URL exclusion set and the org-cue ``(?=…|，|。)``); mapping them
  to ASCII would change boundary semantics — a URL would over-capture past a
  full-width comma. Keeping them full-width preserves exact pre-existing
  behavior. The one value-internal comma in digit groups (``￥９９，８００元``)
  is handled by a targeted ``[,,]`` class in the amount patterns instead.
- NOT full NFKC: compatibility decompositions (ligatures, superscripts,
  circled numbers) change lengths and would break offset alignment.
"""
from __future__ import annotations

_TABLE: dict[int, int] = {}

# Full-width digits and letters -> ASCII (the leak-critical classes).
_TABLE.update({code: code - 0xFEE0 for code in range(0xFF10, 0xFF1A)})  # ０-９
_TABLE.update({code: code - 0xFEE0 for code in range(0xFF21, 0xFF3B)})  # Ａ-Ｚ
_TABLE.update({code: code - 0xFEE0 for code in range(0xFF41, 0xFF5B)})  # ａ-ｚ

# Value-internal punctuation -> half-width (boundary punctuation stays).
_TABLE[0xFF0D] = 0x2D  # － ->
_TABLE[0xFF0E] = 0x2E  # ． -> .
_TABLE[0xFF0F] = 0x2F  # ／ -> /
_TABLE[0xFF1A] = 0x3A  # ： -> :
_TABLE[0xFF05] = 0x25  # ％ -> %
_TABLE[0xFF20] = 0x40  # ＠ -> @
_TABLE[0xFF3F] = 0x5F  # ＿ -> _
_TABLE[0xFF1D] = 0x3D  # ＝ -> =
_TABLE[0xFFE5] = 0xA5  # ￥ -> ¥
_TABLE[0x3000] = 0x20  # ideographic space -> space


def to_halfwidth(text: str) -> str:
    """Translate full-width digits/letters and value-internal punctuation to
    half-width forms (1:1, so offsets in the result equal offsets in the
    input). Boundary punctuation (，。！？…) is intentionally preserved."""
    return text.translate(_TABLE)

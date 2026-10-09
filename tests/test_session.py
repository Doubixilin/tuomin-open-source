"""Session-level concurrency and hydrate-validation regressions.

The legacy ``/session/*`` endpoints (and the CCR reverse proxy) share ONE
SessionRedactor across concurrent requests on the FastAPI thread pool, so
placeholder allocation must be mutually exclusive: a duplicated placeholder
number means one entity's mapping is overwritten and refills to the WRONG
original.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.mapping import PREFIX_BY_LABEL, PlaceholderFactory
from tuomin_gateway.placeholders import PLACEHOLDER_RE
from tuomin_gateway.profiles import get_profile, with_overrides
from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.session import SessionRedactor


def _session() -> SessionRedactor:
    profile = with_overrides(get_profile("kb"), use_ner=False)
    return SessionRedactor([RuleDetector()], profile)


def test_concurrent_mask_allocates_unique_placeholders_and_loses_no_mapping():
    sr = _session()
    # Each text carries a DISTINCT new CONTACT entity, so every mask call must
    # allocate exactly one new placeholder.
    texts = [f"紧急联系人139{i:08d}。" for i in range(160)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        masked = list(pool.map(sr.mask, texts))

    allocated = [ph for output in masked for ph in PLACEHOLDER_RE.findall(output)]
    assert len(allocated) == len(texts)
    assert len(set(allocated)) == len(texts)  # no reused placeholder number
    assert len(sr.mapping) == len(texts)      # no lost mapping entry
    for text, output in zip(texts, masked):
        phone = text.removeprefix("紧急联系人").removesuffix("。")
        (placeholder,) = PLACEHOLDER_RE.findall(output)
        assert sr.mapping[placeholder] == phone  # each maps to ITS entity
        assert sr.unmask(output) == text         # round-trip intact


class _YieldingFactory(PlaceholderFactory):
    """Pauses INSIDE the counter read-modify-write — the exact interleaving the
    language semantics allow (and GIL-free builds would hit for real). Without
    the allocation lock this reliably produces duplicate numbers."""

    def next(self, label: str) -> str:
        prefix = PREFIX_BY_LABEL.get(label, label)
        number = self._counters[prefix] + 1
        time.sleep(0)  # yield mid read-modify-write
        self._counters[prefix] = number
        return f"<{prefix}_{number:03d}>"


def test_concurrent_allocation_stays_unique_when_factory_yields():
    sr = _session()
    sr._factory = _YieldingFactory()  # test-only: stress the vulnerable window
    values = [f"示例公司{i:03d}" for i in range(64)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated = list(pool.map(lambda value: sr.mask_value(value, "ORG"), values))

    assert len(set(allocated)) == len(values)
    assert len(sr.mapping) == len(values)
    for value, placeholder in zip(values, allocated):
        assert sr.mapping[placeholder] == value


def _entry(placeholder: str) -> MappingEntry:
    return MappingEntry(
        placeholder=placeholder, label="ORG", original_value="示例集团", text_hash="h"
    )


def test_hydrate_rejects_non_canonical_placeholders():
    # <ORG_01> hydrates but then self-locks as "altered" on refill — fail closed
    # at hydrate time instead. Lowercase is not the canonical grammar either.
    for placeholder in ["<ORG_01>", "<org_007>"]:
        with pytest.raises(ValueError, match="invalid persisted placeholder"):
            _session().hydrate([_entry(placeholder)])


def test_hydrate_accepts_canonical_placeholders_and_resumes_counter():
    sr = _session()
    sr.hydrate([_entry("<ORG_007>"), _entry("<DEPT_1000>")])

    assert sr.mapping["<ORG_007>"] == "示例集团"
    # Counters resume past the hydrated numbers, so new allocations never
    # collide with restored ones (including the 4-digit range).
    assert sr.mask_value("新示例公司", "ORG") == "<ORG_008>"
    assert sr.mask_value("新示例部门", "DEPARTMENT") == "<DEPT_1001>"


def test_hydrate_and_mask_can_run_concurrently():
    sr = _session()
    entries = [
        MappingEntry(
            placeholder=f"<ORG_{i:03d}>",
            label="ORG",
            original_value=f"示例集团{i:03d}",
            text_hash="h",
        )
        for i in range(1, 60)
    ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        hydrate = pool.submit(sr.hydrate, entries[:30])
        pool.submit(sr.hydrate, entries[30:]).result()
        hydrate.result()
        masked = list(pool.map(lambda v: sr.mask_value(v, "ORG"), [f"新示例公司{i}" for i in range(8)]))

    assert len(sr.mapping) == len(entries) + len(masked)
    assert len(set(sr.mapping) - {e.placeholder for e in entries}) == len(masked)

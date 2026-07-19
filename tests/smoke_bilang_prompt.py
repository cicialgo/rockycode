"""EN/ZH base-prompt pairing — the bilang drift guard.

English is canonical: ROCKY_SYSTEM_ZH mirrors ROCKY_SYSTEM, and the mirror is
only trustworthy while the sha stamp matches. Editing ROCKY_SYSTEM without
updating the zh twin (and re-stamping) turns a silent stale translation into
this test failing — the exact drift risk that makes shipped translations rot.
"""
import hashlib
from pathlib import Path

from rockycode.prompts.rocky import (
    LANG_ZH,
    ROCKY_SYSTEM,
    ROCKY_SYSTEM_EN_SHA8,
    ROCKY_SYSTEM_ZH,
    with_language,
)

en_sha8 = hashlib.sha256(ROCKY_SYSTEM.encode()).hexdigest()[:8]
assert en_sha8 == ROCKY_SYSTEM_EN_SHA8, (
    f"ROCKY_SYSTEM changed (sha8 {en_sha8}, stamp {ROCKY_SYSTEM_EN_SHA8}): "
    "mirror the edit into ROCKY_SYSTEM_ZH in this same commit, then re-stamp "
    "ROCKY_SYSTEM_EN_SHA8 in rockycode/prompts/rocky.py"
)

# Structural anchors both bases must share (the harness relies on them).
for base, tag in ((ROCKY_SYSTEM, "en"), (ROCKY_SYSTEM_ZH, "zh")):
    assert "# Tools this session" in base, f"{tag} base lost the tools-section pointer"
    assert "amaze!" in base, f"{tag} base lost Rocky's signature"

# Voice decision (cici 2026-07-19): amaze! stays English in the zh voice.
assert "「amaze!」" in ROCKY_SYSTEM_ZH and "「我不知道！我学！」" in ROCKY_SYSTEM_ZH

# The lab file that benched this shape stays byte-identical to the product
# constant — the experiment record must keep describing what actually ships.
lab = (Path(__file__).parent.parent / "prompts" / "rocky-zh-full.txt").read_text()
assert lab == ROCKY_SYSTEM_ZH, "prompts/rocky-zh-full.txt drifted from ROCKY_SYSTEM_ZH"

# zh mode composes: zh base + imperative closer (arm-4 shape), closer intact.
composed = with_language(ROCKY_SYSTEM_ZH, "zh")
assert composed.startswith(ROCKY_SYSTEM_ZH) and composed.endswith(LANG_ZH)
assert "必须使用简体中文" in composed

print("BILANG PROMPT SMOKE OK — amaze!")

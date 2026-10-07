"""Check that every number in an answer was taken from tool results (or the question).

The model is told to quote tool text verbatim; this verifies it mechanically:
- a signed number ("-77%") must appear with the same sign — "佔總變動 77%" does not ground "-77%";
- a percentage must match a percentage; a plain number may match either form;
- thousands separators are ignored.
"""
import re

# digits not glued to latin letters, so names like "3C配件", "30W", "600ml" are skipped
NUMBER = re.compile(r"(?<![A-Za-z\d.])[-+−]?\d[\d,]*(?:\.\d+)?(?![\dA-Za-z])(?:\s*%)?")


def normalize(token: str) -> str:
    """Keep an explicit sign ('+'/'-'), drop separators and spaces."""
    return token.replace(",", "").replace(" ", "").replace("−", "-")


def numbers(text: str) -> list[str]:
    return [normalize(m.group()) for m in NUMBER.finditer(text)]


def evidence_set(*texts: str) -> set[str]:
    out = set()
    for t in texts:
        for n in numbers(t):
            unsigned = n.lstrip("+-")
            out.update({n, unsigned, n.rstrip("%"), unsigned.rstrip("%")})
            if n.startswith("-"):
                out.add("-" + unsigned)
    return out


CLAIM = re.compile(r"顯著|正常波動|波動範圍|異常|流失")


def unsupported_claim(answer: str, evidence_texts: list[str]) -> str | None:
    """A verdict ("顯著" / "正常波動" / "流失" …) needs a tool result that actually judged significance."""
    if not CLAIM.search(answer):
        return None
    if any('"判讀"' in t or '"顯著"' in t for t in evidence_texts):
        return None
    return ("你的回答判斷了是否顯著／正常／流失，但目前的工具結果沒有做這個判斷。"
            "請先呼叫 metric_change 或 explain_change（與去年同期比較用 window=\"year\"），再依其「判讀」作答。")


def ungrounded(answer: str, evidence: set[str]) -> list[str]:
    """Numbers in the answer that do not appear in the evidence."""
    bad = []
    for n in numbers(answer):
        signed = n[0] in "+-"
        bare = n.lstrip("+-")
        pct = n.endswith("%")
        if signed:
            ok = n in evidence  # sign must match: "佔總變動 77%" does not ground "-77%"
        else:
            ok = bare in evidence if pct else (bare in evidence or bare.rstrip("%") in evidence)
            if not ok and not pct and "." not in bare and len(bare) == 1:
                ok = True  # list markers / ordinals
        if not ok:
            bad.append(n)
    return list(dict.fromkeys(bad))

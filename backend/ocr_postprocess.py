"""日文竖排 OCR 后处理: 阅读顺序恢复 + 振假名剥离 + 质量报告。

参考竖排轻小说流水线的常见做法:
    1) 按列聚类 (右 → 左)
    2) 列内自上而下
    3) 几何尺寸识别疑似ルビ并剥离
    4) 输出质量指标供上层决定是否落盘
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field


_KANA_RE = re.compile(r"[\u3040-\u30ffーゝゞ]+")
_KANJI_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class TextLine:
    """一行 OCR 结果 (含几何框)。"""

    text: str
    # 四点或轴对齐均可; 统一成 (x0,y0,x1,y1)
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 1.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def width(self) -> float:
        return max(1.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


@dataclass
class PostprocessResult:
    text: str
    lines_kept: int = 0
    lines_ruby: int = 0
    mean_score: float = 0.0
    columns: int = 0
    ruby_candidates: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""


def box_from_points(pts) -> tuple[float, float, float, float]:
    """把 det poly [[x,y],...] 或 [x0,y0,x1,y1] 转成轴对齐框。"""
    if pts is None:
        return 0.0, 0.0, 0.0, 0.0
    # flat 4
    if len(pts) == 4 and not isinstance(pts[0], (list, tuple)):
        x0, y0, x1, y1 = pts
        return float(x0), float(y0), float(x1), float(y1)
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _mostly_kana(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    core = re.sub(r"[\s\W_0-9]+", "", t, flags=re.UNICODE)
    if not core:
        return False
    if _KANJI_RE.search(core):
        return False
    kana_n = sum(1 for ch in core if ("\u3040" <= ch <= "\u30ff") or ch in "ーゝゞ")
    return (kana_n / len(core)) >= 0.85


def cluster_columns(lines: list[TextLine], gap_ratio: float = 0.6) -> list[list[TextLine]]:
    """按中心 x 聚类为列, 返回右→左的列列表。"""
    if not lines:
        return []
    widths = [ln.width for ln in lines]
    med_w = statistics.median(widths) if widths else 40.0
    thresh = max(12.0, med_w * gap_ratio)

    ordered = sorted(lines, key=lambda ln: -ln.cx)  # 右到左粗排
    cols: list[list[TextLine]] = []
    col_xs: list[float] = []
    for ln in ordered:
        placed = False
        for i, cx in enumerate(col_xs):
            if abs(ln.cx - cx) <= thresh:
                cols[i].append(ln)
                # 更新列中心
                col_xs[i] = statistics.mean(x.cx for x in cols[i])
                placed = True
                break
        if not placed:
            cols.append([ln])
            col_xs.append(ln.cx)

    # 列再按中心 x 从右到左
    paired = sorted(zip(col_xs, cols), key=lambda p: -p[0])
    out: list[list[TextLine]] = []
    for _, col in paired:
        out.append(sorted(col, key=lambda ln: ln.cy))  # 列内自上而下
    return out


def strip_ruby(lines: list[TextLine]) -> tuple[list[TextLine], list[TextLine]]:
    """按相对字号剥离疑似振假名。"""
    if len(lines) < 2:
        return lines, []
    heights = [ln.height for ln in lines]
    med_h = statistics.median(heights)
    kept: list[TextLine] = []
    ruby: list[TextLine] = []
    for ln in lines:
        text = (ln.text or "").strip()
        small = ln.height < med_h * 0.55
        short = len(text) <= 10
        if small and short and _mostly_kana(text):
            ruby.append(ln)
        else:
            kept.append(ln)
    return kept, ruby


def order_vertical_text(lines: list[TextLine]) -> PostprocessResult:
    """竖排阅读顺序还原 + ルビ剥离 + 质量统计。"""
    cleaned = [ln for ln in lines if (ln.text or "").strip()]
    if not cleaned:
        return PostprocessResult(text="", ok=False, reason="no_lines")

    kept, ruby = strip_ruby(cleaned)
    cols = cluster_columns(kept)
    parts: list[str] = []
    for col in cols:
        # 同一列内拼接; 日文竖排 OCR 行常常已是短片段
        buf = "".join((ln.text or "").strip() for ln in col)
        if buf:
            parts.append(buf)
    text = "\n".join(parts).strip()

    scores = [ln.score for ln in kept if ln.score is not None]
    mean_score = float(statistics.mean(scores)) if scores else 0.0
    ruby_texts = [(r.text or "").strip() for r in ruby if (r.text or "").strip()]

    ok, reason = quality_gate(text, mean_score=mean_score, line_count=len(kept))
    return PostprocessResult(
        text=text,
        lines_kept=len(kept),
        lines_ruby=len(ruby),
        mean_score=mean_score,
        columns=len(cols),
        ruby_candidates=ruby_texts[:50],
        ok=ok,
        reason=reason,
    )


_GARBAGE = {
    "それでも、",
    "それはそういうことで、",
    "．．．",
    "...",
    "……",
    "即便如此，",
}


def quality_gate(
    text: str,
    *,
    mean_score: float = 1.0,
    line_count: int = 0,
    min_chars: int = 40,
    min_score: float = 0.45,
) -> tuple[bool, str]:
    t = (text or "").strip()
    if len(t) < min_chars:
        return False, f"too_short:{len(t)}"
    if t in _GARBAGE:
        return False, "garbage_phrase"
    if mean_score and mean_score < min_score:
        return False, f"low_score:{mean_score:.2f}"
    if line_count and line_count < 3 and len(t) < 80:
        return False, "too_few_lines"
    return True, "ok"

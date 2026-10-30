"""Presentation regression: output math without changing saved scientific claims."""
from copy import deepcopy

from agenthub_kernel.papers.guidance_export import display_studies, render_studies


def test_report_and_web_use_the_same_math_presentation_without_rewriting_sources():
    units=[{
        "id":"symbols", "kind":"symbols", "title":"符号", "origin":"source",
        "text":r"权重为 W_t^n，初始密度是 \mu。代码 `x_n` 保持原样。",
        "latex":r"\mathrm{ESS}(W)=\frac{1}{\sum_{n=1}^N(W^n)^2}",
        "reason":r"使用 \mu 的定义。", "depends_on":[],
        "definitions":[{"symbol":r"X_t", "meaning":"隐状态", "domain":r"\mathbb{R}^d"}],
        "sources":[{"chunk_id":"c1","pdf_page":1,"quote":r"Source literal x_n and \mu."}],
    }]
    studies={"p1":{"title":"A teaching example", "depth":"method_read", "units":units}}
    original=deepcopy(studies)
    md=render_studies(studies)
    assert "\n$$\n\\mathrm{ESS}" in md and "\n\\[\n" not in md
    assert "$X_t$：隐状态；范围：$\\mathbb{R}^d$" in md
    assert "$W_t^n$" in md and "$\\mu$" in md
    assert "`x_n`" in md
    assert r"Source literal x_n and \mu." in md
    presentation=display_studies(studies)
    assert presentation["p1"]["units"][0]["display_markdown"] in md
    assert studies==original
    assert "display_markdown" not in studies["p1"]["units"][0]

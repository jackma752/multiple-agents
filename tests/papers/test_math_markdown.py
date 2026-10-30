"""Markdown compatibility examples; no model calls or mathematical rewriting."""
import pytest

from agenthub_kernel.papers.math_markdown import display, prose, symbol


@pytest.mark.parametrize(("source", "expected"), [
    (r"均值 \mu，状态 x_t，定义域 \mathbb{R}^d。", r"均值 $\mu$，状态 $x_t$，定义域 $\mathbb{R}^d$。"),
    (r"变量 x_{t-1} 与 q_\theta。", r"变量 $x_{t-1}$ 与 $q_\theta$。"),
    (r"误差 \epsilon_t^2，维度 d_n。", r"误差 $\epsilon_t^2$，维度 $d_n$。"),
    (r"比例 \frac{x_t}{\mu}，根式 \sqrt[3]{a_n}。", r"比例 $\frac{x_t}{\mu}$，根式 $\sqrt[3]{a_n}$。"),
    (r"密度 \mathcal{N} 与 \Sigma_t。", r"密度 $\mathcal{N}$ 与 $\Sigma_t$。"),
    (r"\(x_t = \mu\) 表示状态。", r"$x_t = \mu$ 表示状态。"),
    (r"先计算 \[x_t=\frac{1}{2}x_{t-1}\] 再比较。", "先计算 \n\n$$\nx_t=\\frac{1}{2}x_{t-1}\n$$\n\n 再比较。"),
    (r"假设 $x_t \in \mathbb{R}^d$，均值 \mu。", r"假设 $x_t \in \mathbb{R}^d$，均值 $\mu$。"),
    ("$$\nx_t=1\n$$", "$$\nx_t=1\n$$"),
    (r"\mu,x_t,\mathbb{R}^d", r"$\mu$,$x_t$,$\mathbb{R}^d$"),
    (r"状态x_t与均值\mu定义在R^d中。", r"状态$x_t$与均值$\mu$定义在$R^d$中。"),
    (r'\(x_t + \text{"example"}\)', r'$x_t + \text{"example"}$'),
    (r"范围：t\ge1。", r"范围：$t\ge1$。"),
    (r"\mathbb{R}^d\to(0,\infty)，连续", r"$\mathbb{R}^d\to(0,\infty)$，连续"),
    (r"P\succ0\iff x_t>0，且\forall t成立。", r"$P\succ0\iff$ $x_t>0$，且$\forall$ t成立。"),
    (r"q_\theta(x_t\mid y_t) 是近似分布。", r"$q_\theta(x_t\mid y_t)$ 是近似分布。"),
    (r"取\tilde V_t与滤过\mathcal F_t。", r"取$\tilde V_t$与滤过$\mathcal F_t$。"),
    (r"\hat x_{t-1},\bar \mu_t,\mathbb R^d", r"$\hat x_{t-1}$,$\bar \mu_t$,$\mathbb R^d$"),
    (r"取\tilde\mu_t，\mathbf{x}_t和\frac{1}2。", r"取$\tilde\mu_t$，$\mathbf{x}_t$和$\frac{1}2$。"),
])
def test_prose_formats_only_unmarked_mathematics(source, expected):
    assert prose(source) == expected
    assert prose(expected) == expected


@pytest.mark.parametrize("source", [
    r"Read the method and compare stable baseline results.",
    r"文件 file_name 与 x_initial_value 保持原名。",
    r"金额 $10 and $20，或者 \$30。",
    r"文件 C:\Users\user_name\x_t.md 和 E:/papers/x_t.pdf。",
    r"相对路径 docs/x_t.md 与 docs\x_t.md。",
    r"查看 https://example.org/x_t?q=\mu 和 <https://example.org/a_b>。",
    r"[变量 x_t](https://example.org/x_t) 与 ![图 x_t](fig_x.png)",
    r"[变量 x_t][ref_1]",
    "[ref_1]: https://example.org/x_t\n",
    r"`x_t = \mu` 和 ``含 ` 的 x_t``。",
    "```latex\nx_t = \\mu\n```\n",
    "~~~python\nx_t = 1\n~~~\n",
    "```python\nx_t = 1\n",
    r'$x_t + \text{"example"}$',
    r'$x_t + \text{https://example.org/x_t} + \text{`code_x`}$',
    r"原文：“x_t 与 \mu”；另一原文：‘x_{t-1}’。",
    r"未知命令 \custom{x_t} 不作数学修复。",
    r"缺参数：\tilde、\hat、\mathcal、\frac，不猜补。",
    r"Do not swallow \tilde velocity or \mathcal Filter into math.",
    'Quoted "x_t and \\mu" and \'x_t\'.',
    "> 原文 x_t 与 \\mu\n> 保留 x_{t-1}\n",
])
def test_code_links_paths_money_and_original_quotations_stay_unchanged(source):
    assert prose(source) == source


def test_opaque_material_does_not_prevent_other_prose_from_rendering():
    source = r"`x_t` 是原代码；说明用 \mu 和 x_{t-1}。"
    assert prose(source) == r"`x_t` 是原代码；说明用 $\mu$ 和 $x_{t-1}$。"


@pytest.mark.parametrize(("source", "expected"), [
    (r"\mu", r"$\mu$"),
    (r"x_t", r"$x_t$"),
    (r"\mathbb{R}^d", r"$\mathbb{R}^d$"),
    (r"$x_t$", r"$x_t$"),
    (r"\(x_t\)", r"$x_t$"),
    ("ESS", "$ESS$"),
    ("Gaussian", "Gaussian"),
    (r"C:\papers\x_t.md", r"C:\papers\x_t.md"),
    (r"状态 x_t 所在的 \mathbb{R}^d", r"状态 $x_t$ 所在的 $\mathbb{R}^d$"),
    (r"\tilde V_t", r"$\tilde V_t$"),
    (r"\mathcal F", r"$\mathcal F$"),
    (r"\tilde", r"\tilde"),
])
def test_symbol_fields_are_not_double_delimited_or_mixed_with_prose(source, expected):
    assert symbol(source) == expected


@pytest.mark.parametrize("source", [
    r"x_t=\frac{1}{2}x_{t-1}",
    r"\[x_t=\frac{1}{2}x_{t-1}\]",
    r"\(x_t=\frac{1}{2}x_{t-1}\)",
    r"$x_t=\frac{1}{2}x_{t-1}$",
    "$$\nx_t=\\frac{1}{2}x_{t-1}\n$$",
])
def test_equation_field_uses_exactly_one_obsidian_compatible_block(source):
    expected = "$$\nx_t=\\frac{1}{2}x_{t-1}\n$$"
    assert display(source) == expected
    assert display(display(source)) == expected


def test_empty_fields_do_not_add_empty_formula_blocks():
    assert prose("") == symbol("") == display("") == ""

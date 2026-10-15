"""Build every README figure, light and dark.

    .venv/bin/python scripts/make_figures.py

Inputs: ``runs/*/results.json`` from the evaluation scripts, the PII leak test and the
load tests. The banner and the animation are drawn here; the animation uses real scores
from the exported ONNX checker on the demo example, and the demo tenant's real
calibrated threshold when ``runs/seed/results.json`` exists.
Outputs go to ``docs/assets/<name>-{light,dark}.*``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.patches import FancyBboxPatch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ASSETS = Path("docs/assets")
RUNS = Path("runs")
DPI = 200
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
GOOD, WARN, BAD = "#0ca30c", "#fab219", "#d03b3b"
VERDICT = {"supported": (GOOD, "✓"), "unsupported": (WARN, "?"), "contradicted": (BAD, "✗")}


@dataclass(frozen=True)
class Theme:
    name: str
    surface: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    empty: str
    series: tuple[str, ...]


THEMES = {
    "light": Theme(
        "light",
        "#fcfcfb",
        "#0b0b0b",
        "#52514e",
        "#898781",
        "#e1e0d9",
        "#c3c2b7",
        "#ecebe6",
        ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#8a5cd1"),
    ),
    "dark": Theme(
        "dark",
        "#1a1a19",
        "#ffffff",
        "#c3c2b7",
        "#898781",
        "#2c2c2a",
        "#383835",
        "#262624",
        ("#3987e5", "#d95926", "#199e70", "#c98500", "#9a73dd"),
    ),
}


def use_theme(mode: str) -> Theme:
    t = THEMES[mode]
    plt.rcParams.update(
        {
            "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
            "font.size": 9.5,
            "figure.facecolor": t.surface,
            "axes.facecolor": t.surface,
            "savefig.facecolor": t.surface,
            "text.color": t.ink,
            "axes.labelcolor": t.ink2,
            "axes.edgecolor": t.axis,
            "xtick.color": t.muted,
            "ytick.color": t.muted,
            "xtick.labelcolor": t.ink2,
            "ytick.labelcolor": t.ink2,
            "grid.color": t.grid,
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "legend.frameon": False,
            "legend.fontsize": 9,
        }
    )
    return t


def title(fig: Any, text: str, sub: str, t: Theme) -> None:
    fig.text(0.012, 0.975, text, ha="left", va="top", fontsize=12.5, weight="bold")
    fig.text(0.012, 0.905, sub, ha="left", va="top", fontsize=9, color=t.ink2)


def save(fig: Any, name: str, mode: str) -> None:
    path = ASSETS / f"{name}-{mode}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(path)


def load(name: str) -> Any:
    path = RUNS / name / "results.json"
    return json.loads(path.read_text()) if path.exists() else None


# ------------------------------------------------------------------------ banner


def banner(mode: str) -> str:
    t = THEMES[mode]
    w, h = 1200, 260
    shapes = []
    # The gate: two posts and a bar, with claims arriving from the left.
    gx = 900
    shapes.append(
        f'<rect x="{gx}" y="52" width="14" height="160" rx="5" fill="{t.ink2}" fill-opacity="0.55"/>'
    )
    shapes.append(
        f'<rect x="{gx + 136}" y="52" width="14" height="160" rx="5" fill="{t.ink2}" fill-opacity="0.55"/>'
    )
    shapes.append(
        f'<rect x="{gx - 6}" y="44" width="162" height="16" rx="6" fill="{t.ink2}" fill-opacity="0.75"/>'
    )
    shapes.append(
        f'<text x="{gx + 75}" y="234" text-anchor="middle" font-family="{FONT}" font-size="14" '
        f'fill="{t.ink2}">recall target 90%</text>'
    )
    claims = [
        ("supported", 600, 80, True),
        ("contradicted", 640, 122, False),
        ("supported", 610, 164, True),
        ("unsupported", 660, 206 - 42, False),
    ]
    rows = [80, 122, 164]
    for (verdict, x, _, passes), y in zip(claims[:3], rows, strict=True):
        color, icon = VERDICT[verdict]
        end = gx + 170 if passes else gx - 140
        shapes.append(
            f'<rect x="{end if passes else x + 40}" y="{y}" width="120" height="30" rx="9" fill="{color}" '
            f'fill-opacity="0.16" stroke="{color}" stroke-width="2"/>'
        )
        bx = end if passes else x + 40
        shapes.append(
            f'<text x="{bx + 11}" y="{y + 21}" font-family="{FONT}" font-size="16" font-weight="700" fill="{t.ink}">{icon}</text>'
        )
        shapes.append(
            f'<rect x="{bx + 32}" y="{y + 11}" width="70" height="8" rx="4" fill="{t.ink2}" fill-opacity="0.35"/>'
        )
        if not passes:
            shapes.append(
                f'<path d="M {bx + 124} {y + 15} L {gx - 8} {y + 15}" stroke="{color}" stroke-width="2" '
                f'stroke-dasharray="5 5"/>'
            )
            shapes.append(
                f'<text x="{gx - 30}" y="{y + 20}" font-family="{FONT}" font-size="15" font-weight="700" fill="{color}">✗</text>'
            )
        else:
            shapes.append(
                f'<path d="M {gx - 120} {y + 15} L {end - 6} {y + 15}" stroke="{color}" stroke-width="2"/>'
            )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="Evidence Gate: checks every claim in a RAG answer against its sources before it reaches a reader, with a recall guarantee per deployment">
  <rect width="{w}" height="{h}" rx="18" fill="{t.surface}"/>
  <text x="56" y="112" font-family="{FONT}" font-size="62" font-weight="700" fill="{t.ink}">Evidence Gate</text>
  <text x="58" y="158" font-family="{FONT}" font-size="22" fill="{t.ink2}">Every claim checked against its sources</text>
  <text x="58" y="188" font-family="{FONT}" font-size="22" fill="{t.ink2}">before a reader sees it, at a recall you choose.</text>
  {"".join(shapes)}
</svg>
"""


# --------------------------------------------------------------------- animation


def demo_audit() -> tuple[list[dict[str, Any]], float, str]:
    """Real scores for the demo answer from the exported ONNX checker."""
    from groundsight import audit
    from groundsight.model import Thresholds

    from evidencegate.onnx_checker import load_checker

    seed = RUNS / "seed" / "results.json"
    threshold, calibration = 0.5, "default"
    if seed.exists():
        s = json.loads(seed.read_text())
        threshold, calibration = float(s["threshold"]), "seeded on SciFact, 90% recall"
    passages = [
        "Aspirin irreversibly inhibits cyclooxygenase-1 and -2 by acetylating a serine residue in the active site, which blocks thromboxane A2 synthesis in platelets.",
        "Aspirin can relieve acute migraine pain; it does not prevent future attacks.",
    ]
    answer = (
        "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue. "
        "This blocks thromboxane production in platelets. "
        "Aspirin also cures migraine permanently after a single dose."
    )
    result = audit(
        answer,
        passages,
        question="What does aspirin inhibit?",
        checker=load_checker("minicheck+nli", "models"),
        thresholds=Thresholds(support=threshold),
    )
    claims = [
        {
            "text": c.text,
            "support": c.support,
            "verdict": c.verdict.value,
            "cause": c.diagnosis.cause.value.replace("_", " ") if c.diagnosis else "",
        }
        for c in result.claims
    ]
    return claims, threshold, calibration


def animate(claims: list[dict[str, Any]], threshold: float, calibration: str, mode: str) -> None:
    t = use_theme(mode)
    fig = plt.figure(figsize=(8.4, 4.4))
    steps: list[tuple[str, int]] = []
    for i in range(len(claims)):
        steps += [("bar", i)] * 4 + [("verdict", i)] * 3
    steps += [("decision", 0)] * 6 + [("queue", 0)] * 8 + [("hold", 0)] * 6

    def draw(frame: int) -> None:
        fig.clear()
        kind, current = steps[frame]
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 52)
        ax.axis("off")
        ax.text(3, 49, "POST /v1/check", fontsize=11, weight="bold", va="top")
        ax.text(3, 45.5, "Question: What does aspirin inhibit?", fontsize=9, color=t.ink2, va="top")
        ax.text(62, 49, f"threshold {threshold:.3f}", fontsize=9.5, weight="bold", va="top")
        ax.text(62, 45.5, calibration, fontsize=8.5, color=t.ink2, va="top")
        done_upto = current if kind in ("bar", "verdict") else len(claims)
        for i, c in enumerate(claims):
            y = 37 - i * 10
            shown = i < done_upto or (i == current and kind in ("bar", "verdict"))
            text = c["text"] if len(c["text"]) < 62 else c["text"][:59] + "..."
            ax.text(3, y, text, fontsize=9, va="center", color=t.ink if shown else t.muted)
            # Support bar against the threshold.
            ax.add_patch(
                FancyBboxPatch(
                    (3, y - 4.6),
                    56,
                    1.6,
                    boxstyle="round,pad=0,rounding_size=0.8",
                    facecolor=t.empty,
                    edgecolor="none",
                )
            )
            if shown:
                progress = (
                    1.0
                    if (i < current or kind != "bar" or i != current)
                    else min(1.0, (steps[: frame + 1].count(("bar", i))) / 4)
                )
                color = VERDICT[c["verdict"]][0] if (i < current or kind != "bar") else t.series[0]
                ax.add_patch(
                    FancyBboxPatch(
                        (3, y - 4.6),
                        max(0.4, 56 * c["support"] * progress),
                        1.6,
                        boxstyle="round,pad=0,rounding_size=0.8",
                        facecolor=color,
                        edgecolor="none",
                    )
                )
            ax.plot([3 + 56 * threshold] * 2, [y - 5.6, y - 2.0], color=t.ink, lw=1.2)
            reveal = (
                i < current
                or (i == current and kind == "verdict")
                or kind not in ("bar", "verdict")
            )
            if reveal and shown:
                color, icon = VERDICT[c["verdict"]]
                ax.add_patch(
                    FancyBboxPatch(
                        (62, y - 2.2),
                        21,
                        4.4,
                        boxstyle="round,pad=0,rounding_size=1.2",
                        facecolor=color,
                        alpha=0.18,
                        edgecolor=color,
                        lw=1.4,
                    )
                )
                ax.text(63.2, y, f"{icon} {c['verdict']}", fontsize=9, weight="bold", va="center")
                ax.text(
                    85, y, f"support {c['support']:.2f}", fontsize=8.5, color=t.ink2, va="center"
                )
                if c["verdict"] != "supported" and c["cause"]:
                    ax.text(
                        62,
                        y - 4.2,
                        f"likely cause: {c['cause']}",
                        fontsize=8,
                        color=t.ink2,
                        va="center",
                    )
        if kind in ("decision", "queue", "hold"):
            flagged = [c for c in claims if c["verdict"] != "supported"]
            decision = (
                "PASS"
                if not flagged
                else ("BLOCK" if any(c["verdict"] == "contradicted" for c in flagged) else "FLAG")
            )
            col = GOOD if decision == "PASS" else (BAD if decision == "BLOCK" else WARN)
            ax.add_patch(
                FancyBboxPatch(
                    (3, 2.5),
                    22,
                    6,
                    boxstyle="round,pad=0,rounding_size=1.5",
                    facecolor=col,
                    alpha=0.2,
                    edgecolor=col,
                    lw=1.6,
                )
            )
            ax.text(
                14,
                5.5,
                f"decision: {decision}",
                ha="center",
                va="center",
                fontsize=10.5,
                weight="bold",
            )
        if kind in ("queue", "hold"):
            boxes = [
                ("triage queue", "replay confirms the cause"),
                ("review queue", "labels measure recall"),
                ("audit log", "redacted, hash-chained"),
            ]
            for j, (name, sub) in enumerate(boxes):
                x = 30 + j * 23
                ax.add_patch(
                    FancyBboxPatch(
                        (x, 2.5),
                        21,
                        6,
                        boxstyle="round,pad=0,rounding_size=1.5",
                        facecolor=t.empty,
                        edgecolor=t.axis,
                        lw=1.2,
                    )
                )
                ax.text(x + 10.5, 6.6, name, ha="center", va="center", fontsize=9, weight="bold")
                ax.text(x + 10.5, 4.0, sub, ha="center", va="center", fontsize=7.5, color=t.ink2)
                ax.annotate(
                    "",
                    xy=(x, 5.5),
                    xytext=(x - 2, 5.5),
                    arrowprops={"arrowstyle": "->", "color": t.ink2},
                )

    anim = FuncAnimation(fig, draw, frames=len(steps))
    path = ASSETS / f"gate-{mode}.gif"
    path.parent.mkdir(parents=True, exist_ok=True)
    anim.save(path, writer=PillowWriter(fps=4), dpi=110, savefig_kwargs={"facecolor": t.surface})
    plt.close(fig)
    print(path)


# ------------------------------------------------------------------------ charts

LABELS = {
    "minicheck-int8": "MiniCheck, ONNX int8 (served)",
    "minicheck-fp32": "MiniCheck, ONNX fp32",
    "nli-int8": "DeBERTa-v3 NLI, ONNX int8",
    "embedding-similarity": "Embedding similarity",
    "word-overlap": "Word overlap",
}


def plot_detection(rows: list[dict[str, Any]], mode: str) -> None:
    t = use_theme(mode)
    sets = [
        ("scifact", "SciFact: 1,259 claims vs cited abstracts"),
        ("ragtruth", "RAGTruth: 2,035 answer sentences"),
        ("medhallu", "MedHallu: 2,000 answers (labels doubtful)"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(11.6, 3.5), sharey=True)
    order = [k for k in LABELS if any(r["checker"] == k for r in rows)]
    for ax, (key, name) in zip(axes, sets, strict=True):
        data = {r["checker"]: r for r in rows if r["set"] == key}
        for y, k in enumerate(order[::-1]):
            if k not in data:
                continue
            r = data[k]
            color = (
                t.series[0]
                if k.startswith("minicheck")
                else (t.series[1] if k in ("embedding-similarity", "word-overlap") else t.series[2])
            )
            ax.plot(r["ci95"], [y, y], color=color, lw=2.2, solid_capstyle="round")
            ax.plot(r["auroc"], y, "o", color=color, ms=7)
            ax.text(
                r["ci95"][1] + 0.015,
                y,
                f"{r['auroc']:.3f}",
                va="center",
                fontsize=8.5,
                color=t.ink2,
            )
        ax.axvline(0.5, color=t.axis, lw=1, ls="--")
        ax.set_xlim(0.3, 1.0)
        ax.set_title(name, fontsize=9.5, loc="left", color=t.ink)
        ax.grid(axis="x")
        ax.set_xlabel("AUROC (95% bootstrap interval)")
    axes[0].set_yticks(range(len(order)), [LABELS[k] for k in order[::-1]])
    axes[0].tick_params(axis="y", length=0)
    fig.subplots_adjust(left=0.19, right=0.985, top=0.77, bottom=0.17, wspace=0.1)
    title(
        fig,
        "Does the gate catch unsupported claims?",
        "0.5 is chance. On MedHallu even the best checker is weak: its reference answers"
        " are often not stated in the passages it provides.",
        t,
    )
    save(fig, "detection", mode)


def plot_guarantee(res: dict[str, Any], mode: str) -> None:
    t = use_theme(mode)
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.4))
    names = {"scifact": "SciFact", "medhallu": "MedHallu"}
    ax.plot([0.75, 1.0], [0.75, 1.0], color=t.axis, lw=1, ls="--", label="achieved = target")
    for i, (key, rows) in enumerate(res["guarantee"].items()):
        color = t.series[0] if i == 0 else t.series[1]
        targets = [r["target"] for r in rows]
        ax.plot(
            targets,
            [r["recall_mean"] for r in rows],
            "o-",
            color=color,
            lw=2,
            label=f"{names.get(key, key)}: mean",
        )
        ax.plot(
            targets,
            [r["recall_p5"] for r in rows],
            "o:",
            color=color,
            lw=1.3,
            alpha=0.7,
            label=f"{names.get(key, key)}: 5th percentile",
        )
        ax2.plot(
            targets,
            [r["false_alarm_mean"] for r in rows],
            "o-",
            color=color,
            lw=2,
            label=names.get(key, key),
        )
        for r in rows:
            ax2.text(
                r["target"],
                r["false_alarm_mean"] + 0.03,
                f"{r['false_alarm_mean']:.0%}",
                ha="center",
                fontsize=8,
                color=t.ink2,
            )
    ax.set_xlim(0.77, 0.98)
    ax.set_ylim(0.72, 1.0)
    ax.set_xlabel("target recall")
    ax.set_ylabel("achieved recall (held-out half)")
    ax.grid()
    ax.legend(loc="lower right", fontsize=8)
    ax2.set_xlim(0.77, 0.98)
    ax2.set_ylim(0, 1)
    ax2.set_xlabel("target recall")
    ax2.set_ylabel("supported claims flagged anyway")
    ax2.grid()
    ax2.legend(loc="upper left", fontsize=8)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.77, bottom=0.15, wspace=0.28)
    title(
        fig,
        "The recall promise holds on average; the checker decides what it costs",
        "Conformal thresholds fitted on one half, measured on the other, 200 random splits.",
        t,
    )
    save(fig, "guarantee", mode)


def plot_review(res: dict[str, Any], mode: str) -> None:
    t = use_theme(mode)
    q = res["review_queue"]
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    rows = [
        ("true recall", q["true_recall"], t.ink2),
        ("weighted estimate", q["weighted_estimate_mean"], t.series[2]),
        ("naive estimate (labels only)", q["naive_estimate_mean"], t.series[1]),
    ]
    for y, (_name, v, color) in enumerate(rows):
        ax.barh(y, v, color=color, height=0.55)
        ax.text(v + 0.004, y, f"{v:.3f}", va="center", fontsize=9, color=t.ink2)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0.8, 1.02)
    ax.grid(axis="x")
    ax.set_xlabel("recall at the 90% threshold")
    fig.subplots_adjust(left=0.33, right=0.97, top=0.72, bottom=0.2)
    title(
        fig,
        "Reviewer labels alone overstate recall",
        f"Every flag reviewed, {q['audit_rate']:.0%} of passes audited, {q['replays']} replays."
        f" The weighted 95% interval covered the truth in {q['interval_coverage']:.0%}.",
        t,
    )
    save(fig, "review", mode)


def plot_shift(res: dict[str, Any], mode: str) -> None:
    t = use_theme(mode)
    names = {"scifact": "SciFact", "medhallu": "MedHallu"}
    sets = res["sets"]
    fig, axes = plt.subplots(1, len(sets), figsize=(9.4, 3.4), sharey=True)
    target = res["target_recall"]
    for ax, (key, r) in zip(axes, sets.items(), strict=True):
        curve = [c for c in r["recalibration"] if c["recall_mean"] is not None]
        ks = [c["labels"] for c in curve]
        tr = r["transfer"]
        ax.axhline(target, color=t.ink, lw=1, ls="--")
        ax.plot(
            ks,
            [c["recall_mean"] for c in curve],
            "o-",
            color=t.series[2],
            lw=2,
            label="refit on k target labels (mean)",
        )
        ax.plot(
            ks,
            [c["recall_p10"] for c in curve],
            "o:",
            color=t.series[2],
            lw=1.3,
            alpha=0.7,
            label="10th percentile",
        )
        ax.axhline(
            tr["recall_on_target"],
            color=t.series[1],
            lw=2,
            label=f"RAGTruth threshold as is: {tr['recall_on_target']:.1%}",
        )
        ax.set_xscale("log")
        ax.set_xticks(ks, [str(k) for k in ks])
        ax.minorticks_off()
        ax.set_title(
            f"{names.get(key, key)} (PSI {tr['psi_source_vs_target']:.2f})",
            loc="left",
            fontsize=9.5,
        )
        ax.set_xlabel("labelled target items used to refit")
        ax.grid()
        ax.legend(loc="lower right", fontsize=8)
    axes[0].set_ylabel("recall on held-out items")
    axes[0].set_ylim(0.7, 0.97)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.77, bottom=0.15, wspace=0.08)
    title(
        fig,
        "A borrowed threshold may or may not survive a shift",
        "Threshold for 90% recall fitted on RAGTruth. It held on SciFact and fell short on"
        " MedHallu, where 50 labels restored the mean.",
        t,
    )
    save(fig, "shift", mode)


SYSTEMS = {
    "bm25 without stemming (rank-bm25)": "BM25, unstemmed",
    "lexical": "BM25, stemmed",
    "dense": "dense",
    "hybrid": "hybrid (RRF)",
    "hybrid_rerank": "hybrid + reranker",
}


def plot_retrieval(res: dict[str, Any], mode: str) -> None:
    t = use_theme(mode)
    rows = res["results"]
    fig, ax = plt.subplots(figsize=(7.8, 3.4))
    names = [SYSTEMS.get(r["system"], r["system"]) for r in rows]
    metrics = [("recall@10", t.series[0]), ("ndcg@10", t.series[2])]
    width = 0.38
    for j, (m, color) in enumerate(metrics):
        xs = [i + (j - 0.5) * width for i in range(len(rows))]
        ax.bar(xs, [r[m] for r in rows], width=width * 0.92, color=color, label=m)
        for x, r in zip(xs, rows, strict=True):
            ax.text(x, r[m] + 0.01, f"{r[m]:.3f}", ha="center", fontsize=8, color=t.ink2)
    labels = [f"{n}\n{r['p50_ms']:.0f} ms" for n, r in zip(names, rows, strict=True)]
    ax.set_xticks(range(len(rows)), labels, fontsize=8.5)
    ax.set_ylim(0, max(r["ndcg@10"] for r in rows) * 1.2)
    ax.grid(axis="y")
    ax.legend(loc="upper left", ncol=2)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.77, bottom=0.18)
    title(
        fig,
        "Stemmed BM25 in Postgres matches hybrid retrieval with reranking",
        f"BioASQ, {res['questions']} questions over {res['passages']:,} PubMed abstracts;"
        " median latency under each bar. Reranking is opt-in.",
        t,
    )
    save(fig, "retrieval", mode)


PII_KINDS = {
    "first_name": "first name",
    "last_name": "last name",
    "address": "street address",
    "dob": "date of birth",
    "phone": "phone",
    "email": "email",
    "ssn": "SSN",
    "mrn": "record number",
    "ip": "IP address",
}


def plot_pii(runs: list[tuple[str, dict[str, Any]]], mode: str) -> None:
    t = use_theme(mode)
    fig, ax = plt.subplots(figsize=(8.4, 3.3))
    kinds = list(PII_KINDS)
    width = 0.8 / len(runs)
    for j, (name, res) in enumerate(runs):
        xs = [i + (j - (len(runs) - 1) / 2) * width for i in range(len(kinds))]
        vals = [res["by_kind"].get(k, {}).get("leaked", 0) for k in kinds]
        ax.bar(
            xs,
            vals,
            width=width * 0.92,
            color=t.series[[1, 3, 2][j % 3]],
            label=f"{name}: {res['identifiers_leaked']:,} leaked",
        )
    ax.set_xticks(range(len(kinds)), [PII_KINDS[k] for k in kinds], fontsize=8.5)
    ax.set_ylabel("identifiers found in a sink")
    ax.grid(axis="y")
    ax.legend(loc="upper right", fontsize=8.5)
    seeded = runs[0][1]["identifiers_seeded"]
    fig.subplots_adjust(left=0.09, right=0.98, top=0.77, bottom=0.14)
    title(
        fig,
        "PII leak test: what reaches storage, the queue, logs and traces",
        f"{runs[0][1]['requests']:,} requests carrying {seeded:,} synthetic identifiers;"
        " each run scans every sink afterwards.",
        t,
    )
    save(fig, "pii", mode)


def plot_load(runs: list[tuple[str, dict[str, Any]]], mode: str) -> None:
    t = use_theme(mode)
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.4))
    for j, (name, res) in enumerate(runs):
        rows = res["steps"]
        color = t.series[j % len(t.series)]
        vus = [r["vus"] for r in rows]
        ax.plot(vus, [r["rps"] for r in rows], "o-", color=color, lw=2, label=name)
        ax2.plot(vus, [r["p95_ms"] / 1000 for r in rows], "o-", color=color, lw=2, label=name)
    for a in (ax, ax2):
        a.set_xscale("log", base=2)
        a.set_xlabel("concurrent users")
        a.grid()
        xs = sorted({r["vus"] for _, res in runs for r in res["steps"]})
        a.set_xticks(xs, [str(x) for x in xs])
    ax.set_ylabel("checks per second")
    ax.set_ylim(0, None)
    ax2.set_ylabel("p95 latency (s)")
    ax2.set_ylim(0, None)
    ax.legend(loc="lower right", fontsize=8)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.77, bottom=0.15, wspace=0.25)
    title(
        fig,
        "Checker replicas add throughput once calls are balanced per request",
        runs[0][1].get("where", "") + "; every request is new to the score cache.",
        t,
    )
    save(fig, "load", mode)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for mode in THEMES:
        (ASSETS / f"banner-{mode}.svg").write_text(banner(mode))
        print(ASSETS / f"banner-{mode}.svg")
    claims, threshold, calibration = demo_audit()
    pii = [
        (name, r)
        for name, key in (
            ("first version, small model", "pii-first-version"),
            ("fixes, small model", "pii-small-model"),
            ("fixes, large model (default)", "pii"),
        )
        if (r := load(key)) is not None
    ]
    load_runs = [
        (name, r)
        for name, key in (
            ("1 checker", "load-1-checker"),
            ("2 checkers, one connection (pick_first)", "load-2-checkers-pick-first"),
            ("2 checkers, per-call round robin", "load-2-checkers"),
        )
        if (r := load(key)) is not None
    ]
    for mode in THEMES:
        animate(claims, threshold, calibration, mode)
        if (d := load("detection")) is not None:
            plot_detection(d, mode)
        if (g := load("guarantee")) is not None:
            plot_guarantee(g, mode)
            plot_review(g, mode)
        if (s := load("shift")) is not None:
            plot_shift(s, mode)
        if (r := load("retrieval")) is not None:
            plot_retrieval(r, mode)
        if pii:
            plot_pii(pii, mode)
        if load_runs:
            plot_load(load_runs, mode)


if __name__ == "__main__":
    main()

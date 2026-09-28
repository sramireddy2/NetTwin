"""Render a recording from `scripts/record_demo.py` as an animated terminal SVG.

The SVG is plain markup plus CSS keyframes, so GitHub plays it inline in the README and it
stays sharp at any zoom. Lines appear in the order and rhythm they were printed; waits longer
than a second (mostly the model thinking) are shortened, which the title bar says. Viewers who
ask for reduced motion get the final screen without the animation.

    uv run python scripts/render_demo.py docs/media/demo-019.json docs/media/demo-019.svg
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import typer

FONT_SIZE = 14
CHAR_W = 8.6  # a little wider than 0.6 em, so a wider fallback font still fits
LINE_H = 20
PAD_X = 18
PAD_Y = 14
BAR_H = 36
FONTS = "'Cascadia Mono','SF Mono',Menlo,Consolas,'Liberation Mono','DejaVu Sans Mono',monospace"
BOLD = ("bright", "ok", "bad")


def shown_times(events: list[dict[str, Any]], max_gap: float, lead: float) -> list[float]:
    """Replay time of each line: real gaps up to `max_gap`, longer waits shortened."""
    times: list[float] = []
    prev_real = events[0]["t"] if events else 0.0
    clock = lead
    for event in events:
        gap = max(0.0, event["t"] - prev_real)
        if gap > max_gap:
            gap = max_gap + min(0.9, 0.3 * math.log10(gap / max_gap))
        clock += max(gap, 0.05)
        times.append(round(clock, 3))
        prev_real = event["t"]
    return times


def keyframes(name: str, stops: list[tuple[float, str]], total: float) -> str:
    """One @keyframes rule whose values hold until the next stop (steps(1, end))."""
    parts = []
    last = None
    for at, value in stops:
        if value == last:
            continue
        pct = min(100.0, 100.0 * at / total)
        parts.append(f"{pct:.3f}%{{transform:{value}}}")
        last = value
    parts.append(f"100%{{transform:{last}}}")
    return f"@keyframes {name}{{{''.join(parts)}}}"


def render(recording: dict[str, Any], rows: int, max_gap: float, hold: float) -> str:
    palette: dict[str, str] = recording["palette"]
    events: list[dict[str, Any]] = recording["events"]
    cols = int(recording.get("cols", 100))
    width = round(cols * CHAR_W + 2 * PAD_X)
    height = BAR_H + PAD_Y * 2 + rows * LINE_H
    type_time = 1.4
    times = shown_times(events, max_gap, lead=0.8)
    # The first line (the command) is typed; everything after it waits for the typing.
    times = [times[0]] + [t + type_time for t in times[1:]]
    total = round(times[-1] + hold, 3)

    # Scrolling strip: line i sits at y = i * LINE_H; the curtain hides lines not yet printed.
    curtain = [(0.0, "translateY(0px)")]
    scroll = [(0.0, "translateY(0px)")]
    for i, at in enumerate(times):
        shown = i + 1
        curtain.append((at, f"translateY({shown * LINE_H}px)"))
        scroll.append((at, f"translateY({-max(0, shown - rows) * LINE_H}px)"))
    final_curtain = curtain[-1][1]
    final_scroll = scroll[-1][1]

    first = events[0]["segments"] if events else []
    prompt_len = len(first[0][1]) if first else 0
    typed = sum(len(text) for _, text in first[1:])
    start, end = times[0] if times else 0.0, (times[0] if times else 0.0) + type_time
    typer_stops = (
        f"0%{{transform:translateX(0px)}}"
        f"{100 * start / total:.3f}%{{transform:translateX(0px);"
        f"animation-timing-function:steps({max(1, typed)},end)}}"
        f"{100 * end / total:.3f}%{{transform:translateX({typed * CHAR_W:.1f}px)}}"
        f"100%{{transform:translateX({typed * CHAR_W:.1f}px)}}"
    )

    styles = "".join(
        f".s-{name}{{fill:{color}}}" for name, color in palette.items() if name != "bg"
    )
    css = (
        f"text{{font-family:{FONTS};font-size:{FONT_SIZE}px;white-space:pre}}"
        f"{styles}.b{{font-weight:700}}.sm{{font-size:12px}}"
        f".strip{{transform:{final_scroll};animation:scroll {total}s steps(1,end) infinite}}"
        f".curtain{{transform:{final_curtain};animation:curtain {total}s steps(1,end) infinite}}"
        f".typer{{transform:translateX({typed * CHAR_W:.1f}px);"
        f"animation:type {total}s steps(1,end) infinite}}"
        + keyframes("scroll", scroll, total)
        + keyframes("curtain", curtain, total)
        + f"@keyframes type{{{typer_stops}}}"
        + "@media (prefers-reduced-motion:reduce){.strip,.curtain,.typer{animation:none}}"
    )

    lines = []
    for i, event in enumerate(events):
        y = i * LINE_H + LINE_H - 5
        spans = "".join(
            f'<tspan class="s-{style}{" b" if style in BOLD else ""}">{escape(text)}</tspan>'
            for style, text in event["segments"]
            if text
        )
        lines.append(f'<text x="0" y="{y}" xml:space="preserve">{spans}</text>')

    bg = palette.get("bg", "#0d1117")
    title = escape(recording.get("title", "nettwin"))
    body_w = width - 2 * PAD_X
    body_h = rows * LINE_H
    strip_h = max(len(events), rows) * LINE_H + LINE_H
    typer_x = prompt_len * CHAR_W
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">'
        f'<title id="t">{title}</title>'
        f'<desc id="d">A real NetTwin run replayed as a terminal: a fault is planted on the '
        f"twin, the Claude Code agent team investigates and fixes it, an isolated verifier "
        f"checks the fix, and the harness scores the result.</desc>"
        f"<style>{css}</style>"
        f'<rect width="{width}" height="{height}" rx="10" fill="{bg}"/>'
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" '
        f'fill="none" stroke="#30363d"/>'
        f'<path d="M0.5 {BAR_H}H{width - 0.5}" stroke="#30363d"/>'
        f'<circle cx="20" cy="{BAR_H / 2}" r="6" fill="#ff5f57"/>'
        f'<circle cx="40" cy="{BAR_H / 2}" r="6" fill="#febc2e"/>'
        f'<circle cx="60" cy="{BAR_H / 2}" r="6" fill="#28c840"/>'
        f'<text x="{width / 2}" y="{BAR_H / 2 + 5}" text-anchor="middle" '
        f'class="s-dim">{title}</text>'
        f'<text x="{width - 16}" y="{BAR_H / 2 + 5}" text-anchor="end" class="s-dim sm">'
        f"real run · long waits shortened</text>"
        f'<svg x="{PAD_X}" y="{BAR_H + PAD_Y}" width="{body_w}" height="{body_h}">'
        f'<g class="strip">{"".join(lines)}'
        f'<rect class="typer" x="{typer_x:.1f}" y="0" width="{body_w}" height="{LINE_H}" '
        f'fill="{bg}"/>'
        f'<rect class="curtain" x="0" y="0" width="{body_w}" height="{strip_h}" fill="{bg}"/>'
        f"</g></svg></svg>\n"
    )


def main(
    recording: Path = typer.Argument(..., help="JSON written by scripts/record_demo.py"),
    out: Path = typer.Argument(..., help="SVG to write"),
    rows: int = typer.Option(28, help="Visible terminal rows"),
    max_gap: float = typer.Option(1.0, help="Longest pause kept as it was, in seconds"),
    hold: float = typer.Option(8.0, help="Seconds the last screen stays before the loop"),
) -> None:
    data = json.loads(recording.read_text(encoding="utf-8"))
    svg = render(data, rows, max_gap, hold)
    out.write_text(svg, encoding="utf-8", newline="\n")
    print(f"{out}: {len(data['events'])} lines, {len(svg) // 1024} KiB")


if __name__ == "__main__":
    typer.run(main)

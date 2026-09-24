"""A callable demo and a standalone report of actual simulated execution."""

from datetime import datetime
from html import escape
import json
from pathlib import Path
from .engine import Engine
from .simulator import Simulator

LABELS = {
    "robot_1": "机械臂",
    "scanner_1": "扫码器",
    "liquid_handler_1": "移液站",
    "sealer_1": "封膜机",
    "shaker_1": "振荡器",
    "reader_1": "荧光检测仪",
    "fridge_1": "冰箱",
    "peeler_1": "撕膜机",
    "centrifuge_1": "离心机",
    "stack_1": "耗材堆栈",
}


def write_timeline(report, workflow, path):
    """Render recorded command intervals, retaining unknown intervals visibly."""
    steps = {s["id"]: s for s in workflow["steps"]}
    devices = list(LABELS)
    height = 80 + len(devices) * 55
    duration = max(1, report["sim_time"])
    scale = 850 / duration
    svg = [
        f'<svg viewBox="0 0 1100 {height}" role="img" aria-label="设备实际模拟执行时间线">'
    ]
    for i in range(11):
        x = 180 + 85 * i
        svg.append(
            f'<path d="M{x} 35 V{height-20}" stroke="#e3e9ee"/><text x="{x}" y="22" font-size="12">{duration*i/10:.1f}s</text>'
        )
    for i, device in enumerate(devices):
        y = 50 + i * 55
        svg.append(
            f'<text x="8" y="{y+22}" font-size="14">{escape(LABELS[device])}</text>'
        )
        for cmd in report["commands"]:
            step = steps[cmd["step_id"]]
            if step["device_id"] != device:
                continue
            start = cmd["started"]
            end = cmd["ended"] if cmd["ended"] is not None else report["sim_time"]
            width = max(5, (end - start) * scale)
            color = (
                "#e09126"
                if cmd["state"] != "succeeded"
                else ("#167d9a" if step["plate_id"] == "plate_01" else "#785fc0")
            )
            title = escape(f"{step['id']} | {start:g}–{end:g}s | {cmd['state']}")
            svg.append(
                f'<rect x="{180+start*scale:.2f}" y="{y}" width="{width:.2f}" height="32" rx="4" fill="{color}"><title>{title}</title></rect>'
            )
    svg.append("</svg>")
    positions = "".join(
        f'<tr><td>{escape(plate)}</td><td>{escape(location)}</td><td>{escape(report["certainty"][plate])}</td></tr>'
        for plate, location in report["positions"].items()
    )
    alarms = (
        "".join(
            f'<li>{escape(a["reason"])} · {escape(a["state"])}</li>'
            for a in report["alarms"]
        )
        or "<li>无设备报警</li>"
    )
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>实验室排程 · 模拟执行报告</title><style>
body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f3f6f9;color:#203346;max-width:1200px;margin:40px auto;padding:0 24px}
h1{font-size:28px}p{line-height:1.7}.card{background:white;border:1px solid #dde5eb;border-radius:12px;padding:24px;margin:20px 0}
.badge{color:#167d9a;font-weight:700}.chart{overflow:auto}svg{min-width:850px;width:100%}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #e3e9ee;padding:12px}
small{color:#687b8c}.legend span{display:inline-block;margin-right:24px}a{color:#167d9a}
</style><h1>实验室排程 · 模拟执行报告</h1><p>双板协同：堆栈 → 扫码 → 移液 → 封膜 → 振荡 → 检测 → 入库</p>"""
    html += f'<div class="card"><span class="badge">状态：{escape(report["state"])}</span><p>虚拟时间 {report["sim_time"]:g} 秒 · 已派发 {len(report["commands"])} 个动作 · {len(report["events"])} 条审计事件</p><small>这是软件模拟结果，时间和检测数值不代表真实设备性能。蓝色为 plate_01，紫色为 plate_02，橙色为未确认动作。鼠标悬停可查看步骤。</small></div>'
    html += '<div class="card chart"><h2>实际执行甘特图</h2>' + "".join(svg) + "</div>"
    html += (
        '<div class="card"><h2>板位记录</h2><p>unknown 表示位置未确认，此时下面的位置仅为最后已知位置。</p><table><tr><th>板</th><th>位置</th><th>确认状态</th></tr>'
        + positions
        + "</table></div>"
    )
    html += (
        '<div class="card"><h2>报警</h2><ul>'
        + alarms
        + '</ul><a href="report.json">完整 JSON 记录</a></div></html>'
    )
    Path(path).write_text(html, encoding="utf-8")


def run_demo(output_dir, faults=None):
    """Run the two-plate example and save JSON, HTML and both SQLite databases.

    Args:
        output_dir: New directory; existing paths are rejected to preserve runs.
        faults: Optional step-id mapping to 'lost_ack' or 'unknown'.
    Returns:
        Serializable final snapshot (succeeded or blocked).
    """
    root = Path(__file__).resolve().parents[1]
    cell = json.loads((root / "examples/workcell.json").read_text(encoding="utf-8"))
    flow = json.loads((root / "examples/workflow.json").read_text(encoding="utf-8"))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    sim = Simulator(output / "devices.sqlite", faults=faults)
    engine = None
    try:
        engine = Engine(cell, output / "runtime.sqlite", sim)
        engine.submit(flow, "demo")
        result = engine.run_until_complete()
        (output / "report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        write_timeline(result, flow, output / "timeline.html")
        return result
    finally:
        if engine is not None:
            engine.close()
        sim.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a software-only laboratory workcell demo"
    )
    parser.add_argument(
        "--output",
        default=".runtime/demo-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
    )
    args = parser.parse_args()
    report = run_demo(args.output)
    print(
        json.dumps(
            {
                "state": report["state"],
                "virtual_seconds": report["sim_time"],
                "commands": len(report["commands"]),
                "timeline": str((Path(args.output) / "timeline.html").resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标普500指数基金每日定投跟踪器

数据源
    天天基金（东方财富）历史净值接口
    https://fund.eastmoney.com/pingzhongdata/{code}.js

定投规则
    - 每个交易日按固定金额申购，确认份额 = 金额 / 当日单位净值
    - 非交易日不申购（以官方披露净值的日期为准）
    - 起始日期由 config.json 指定（2026-08-17），无终止日期

输出
    data/portfolio.json   当前持仓与统计快照
    data/timeline.json    逐日累计明细
    reports/curve.svg     定投曲线（供 README 引用）
    reports/index.html    可视化看板
    README.md             仓库首页状态
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

CST = timezone(timedelta(hours=8))
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://fund.eastmoney.com/",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 数据抓取
# --------------------------------------------------------------------------- #
def http_get(url: str, retries: int = 4, timeout: int = 30) -> str:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"  请求失败（第 {attempt}/{retries} 次）：{exc}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"无法获取 {url}：{last_err}")


def fetch_fund(code: str) -> tuple[str, list[tuple[str, float]]]:
    """返回 (基金名称, [(日期, 单位净值), ...] 升序)。"""
    text = http_get(PINGZHONG_URL.format(code=code))

    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not match:
        raise RuntimeError(f"未找到基金 {code} 的净值序列")

    name_match = re.search(r'var\s+fS_name\s*=\s*"([^"]*)"', text)
    name = name_match.group(1) if name_match else code

    navs: list[tuple[str, float]] = []
    for point in json.loads(match.group(1)):
        # 接口时间戳为「北京时间零点」对应的 UTC 毫秒值，需按 UTC+8 还原日期
        day = datetime.fromtimestamp(point["x"] / 1000, CST).strftime("%Y-%m-%d")
        navs.append((day, float(point["y"])))

    navs.sort(key=lambda item: item[0])
    log(f"  {code} {name}：共 {len(navs)} 条净值，最新 {navs[-1][0]} = {navs[-1][1]}")
    return name, navs


# --------------------------------------------------------------------------- #
# 定投计算
# --------------------------------------------------------------------------- #
def compute_fund(fund_cfg: dict, navs: list[tuple[str, float]], start_date: str) -> dict:
    amount = float(fund_cfg["daily_amount"])
    shares = 0.0
    invested = 0.0
    daily: list[dict] = []

    for day, nav in navs:
        if day < start_date:
            continue
        bought = amount / nav
        shares += bought
        invested += amount
        daily.append(
            {
                "date": day,
                "nav": round(nav, 4),
                "amount": round(amount, 2),
                "shares": round(bought, 4),
                "cum_shares": round(shares, 4),
                "cum_invested": round(invested, 2),
                "market_value": round(shares * nav, 2),
            }
        )

    latest_nav = daily[-1]["nav"] if daily else 0.0
    market_value = shares * latest_nav
    profit = market_value - invested

    return {
        "code": fund_cfg["code"],
        "name": fund_cfg["name"],
        "short_name": fund_cfg.get("short_name", fund_cfg["name"]),
        "share_class": fund_cfg.get("share_class", ""),
        "daily_amount": round(amount, 2),
        "trading_days": len(daily),
        "shares": round(shares, 2),
        "invested": round(invested, 2),
        "avg_cost": round(invested / shares, 4) if shares else 0.0,
        "latest_nav": round(latest_nav, 4),
        "latest_nav_date": daily[-1]["date"] if daily else "",
        "market_value": round(market_value, 2),
        "profit": round(profit, 2),
        "return_rate": round(profit / invested, 6) if invested else 0.0,
        "daily": daily,
    }


def build_timeline(fund_results: list[dict]) -> list[dict]:
    """合并各基金为组合层面的逐日累计曲线。"""
    index = {f["code"]: {r["date"]: r for r in f["daily"]} for f in fund_results}
    dates = sorted({d for f in fund_results for d in index[f["code"]]})

    state = {f["code"]: {"cum_invested": 0.0, "cum_shares": 0.0, "nav": 0.0} for f in fund_results}
    timeline: list[dict] = []

    for day in dates:
        invested = value = 0.0
        for f in fund_results:
            rec = index[f["code"]].get(day)
            if rec:
                state[f["code"]] = {
                    "cum_invested": rec["cum_invested"],
                    "cum_shares": rec["cum_shares"],
                    "nav": rec["nav"],
                }
            cur = state[f["code"]]
            invested += cur["cum_invested"]
            value += cur["cum_shares"] * cur["nav"]
        timeline.append(
            {
                "date": day,
                "invested": round(invested, 2),
                "market_value": round(value, 2),
                "profit": round(value - invested, 2),
            }
        )
    return timeline


# --------------------------------------------------------------------------- #
# 可视化：定投曲线 SVG
# --------------------------------------------------------------------------- #
def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def render_curve_svg(timeline: list[dict], width: int = 940, height: int = 430) -> str:
    if not timeline:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="15" fill="#64748b">暂无数据</text></svg>'
        )

    pad_l, pad_r, pad_t, pad_b = 70, 26, 54, 52
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(timeline)

    values = [p["invested"] for p in timeline] + [p["market_value"] for p in timeline]
    lo = 0.0
    hi = max(values) * 1.12 if max(values) > 0 else 1.0

    def x_at(i: int) -> float:
        return pad_l + (iw * i / (n - 1) if n > 1 else iw / 2)

    def y_at(v: float) -> float:
        return pad_t + ih * (1 - (v - lo) / (hi - lo))

    def path(key: str) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x_at(i):.1f},{y_at(p[key]):.1f}"
            for i, p in enumerate(timeline)
        )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,BlinkMacSystemFont,'
        f'\'Segoe UI\',\'PingFang SC\',\'Microsoft YaHei\',sans-serif">',
        '<defs>',
        '<linearGradient id="mvFill" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0%" stop-color="#3b82f6" stop-opacity="0.28"/>',
        '<stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>',
        "</linearGradient>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        # 标题与图例
        f'<text x="{pad_l}" y="24" font-size="15" font-weight="600" fill="#0f172a">'
        f"定投累计投入 vs 当前市值</text>",
        f'<line x1="{pad_l}" y1="40" x2="{pad_l+16}" y2="40" stroke="#94a3b8" '
        f'stroke-width="2.5" stroke-dasharray="5 4"/>',
        f'<text x="{pad_l+22}" y="44" font-size="12" fill="#64748b">累计投入</text>',
        f'<line x1="{pad_l+100}" y1="40" x2="{pad_l+116}" y2="40" stroke="#2563eb" stroke-width="2.5"/>',
        f'<text x="{pad_l+122}" y="44" font-size="12" fill="#64748b">当前市值</text>',
    ]

    # 横向网格与 Y 轴刻度
    grid = 5
    for k in range(grid + 1):
        v = lo + (hi - lo) * k / grid
        y = y_at(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+iw}" y2="{y:.1f}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l-10}" y="{y+4:.1f}" text-anchor="end" font-size="11" '
            f'fill="#94a3b8">{_fmt_money(v)}</text>'
        )

    # 面积填充 + 市值线 + 投入线
    area = (
        f"M{x_at(0):.1f},{y_at(lo):.1f} "
        + " ".join(f"L{x_at(i):.1f},{y_at(p['market_value']):.1f}" for i, p in enumerate(timeline))
        + f" L{x_at(n-1):.1f},{y_at(lo):.1f} Z"
    )
    parts.append(f'<path d="{area}" fill="url(#mvFill)"/>')
    parts.append(
        f'<path d="{path("invested")}" fill="none" stroke="#94a3b8" stroke-width="2.5" '
        f'stroke-dasharray="5 4" stroke-linejoin="round"/>'
    )
    parts.append(
        f'<path d="{path("market_value")}" fill="none" stroke="#2563eb" stroke-width="2.8" '
        f'stroke-linejoin="round"/>'
    )

    # 末端圆点
    parts.append(
        f'<circle cx="{x_at(n-1):.1f}" cy="{y_at(timeline[-1]["invested"]):.1f}" r="4" '
        f'fill="#ffffff" stroke="#94a3b8" stroke-width="2.5"/>'
    )
    parts.append(
        f'<circle cx="{x_at(n-1):.1f}" cy="{y_at(timeline[-1]["market_value"]):.1f}" r="4.5" '
        f'fill="#2563eb" stroke="#ffffff" stroke-width="2"/>'
    )

    # X 轴日期
    tick_count = min(7, n)
    seen: set[int] = set()
    for k in range(tick_count):
        i = round((n - 1) * k / (tick_count - 1)) if tick_count > 1 else 0
        if i in seen:
            continue
        seen.add(i)
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{pad_t+ih+22:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#94a3b8">{timeline[i]["date"][5:]}</text>'
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t+ih:.1f}" x2="{pad_l+iw}" y2="{pad_t+ih:.1f}" '
        f'stroke="#cbd5e1" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 渲染：README
# --------------------------------------------------------------------------- #
def _signed(value: float, digits: int = 2) -> str:
    return f"{'+' if value >= 0 else ''}{value:,.{digits}f}"


def _signed_pct(value: float) -> str:
    return f"{'+' if value >= 0 else ''}{value * 100:.2f}%"


def render_readme(config: dict, funds: list[dict], total: dict, timeline: list[dict]) -> str:
    cur = config.get("currency_symbol", "¥")
    latest_date = max((f["latest_nav_date"] for f in funds if f["latest_nav_date"]), default="-")
    updated = datetime.now(CST).strftime("%Y-%m-%d %H:%M (UTC+8)")

    lines: list[str] = []
    lines.append(f"# {config['title']}")
    lines.append("")
    lines.append(
        "> 自动跟踪标普500指数基金（QDII）每日定投，数据取自天天基金官方披露净值，"
        "由 GitHub Actions 每个交易日自动更新。"
    )
    lines.append("")
    lines.append(f"**仓库**：{config['repo']}")
    lines.append("")
    lines.append("## 当前状态")
    lines.append("")
    lines.append(f"- **开始定投**：{config['start_date']}（持续定投，无终止日期）")
    lines.append(f"- **最新净值日**：{latest_date}")
    lines.append(f"- **累计交易日**：{total['trading_days']} 天")
    lines.append(f"- **累计投入**：{cur}{_fmt_money(total['invested'])}")
    lines.append(f"- **当前市值**：{cur}{_fmt_money(total['market_value'])}")
    lines.append(
        f"- **累计盈亏**：{cur}{_signed(total['profit'])}"
        f"（{_signed_pct(total['return_rate'])}）"
    )
    lines.append("")
    lines.append("## 持仓明细")
    lines.append("")
    lines.append("| 基金 | 代码 | 每日定投 | 交易日 | 投入本金 | 持有份额 | 成本净值 | 最新净值 | 当前市值 | 累计盈亏 | 收益率 |")
    lines.append("|:---|:---:|:---:|:---:|---:|---:|---:|---:|---:|---:|---:|")
    for f in funds:
        lines.append(
            f"| {f['short_name']} | {f['code']} | {cur}{f['daily_amount']:.2f} | {f['trading_days']} | "
            f"{cur}{_fmt_money(f['invested'])} | {f['shares']:,.2f} | {f['avg_cost']:.4f} | "
            f"{f['latest_nav']:.4f} | {cur}{_fmt_money(f['market_value'])} | "
            f"{cur}{_signed(f['profit'])} | {_signed_pct(f['return_rate'])} |"
        )
    lines.append(
        f"| **合计** | — | {cur}{total['daily_amount']:.2f} | {total['trading_days']} | "
        f"**{cur}{_fmt_money(total['invested'])}** | **{total['shares']:,.2f}** | — | — | "
        f"**{cur}{_fmt_money(total['market_value'])}** | "
        f"**{cur}{_signed(total['profit'])}** | **{_signed_pct(total['return_rate'])}** |"
    )
    lines.append("")
    lines.append("## 定投曲线")
    lines.append("")
    lines.append("![定投曲线](reports/curve.svg)")
    lines.append("")
    lines.append("## 最近记录")
    lines.append("")
    lines.append("| 日期 | 累计投入 | 当前市值 | 累计盈亏 |")
    lines.append("|:---|---:|---:|---:|")
    for point in timeline[-10:][::-1]:
        lines.append(
            f"| {point['date']} | {cur}{_fmt_money(point['invested'])} | "
            f"{cur}{_fmt_money(point['market_value'])} | {cur}{_signed(point['profit'])} |"
        )
    lines.append("")
    lines.append("## 定投规则")
    lines.append("")
    lines.append("- 每个交易日按固定金额申购，确认份额 = 金额 ÷ 当日单位净值；非交易日不申购。")
    for f in funds:
        lines.append(f"- {f['name']}（{f['code']}）：每个交易日 {cur}{f['daily_amount']:.2f}")
    lines.append(f"- 起投日：{config['start_date']}，无终止日期。")
    lines.append("")
    lines.append("## 数据与自动化")
    lines.append("")
    lines.append(f"- 数据源：{config['source']}（{config['source_url']}）")
    lines.append("- 更新方式：GitHub Actions 定时任务，自动抓取净值、重算持仓并提交结果。")
    lines.append("- 看板页面：`reports/index.html`（可启用 GitHub Pages 在线查看）。")
    lines.append("")
    lines.append("---")
    lines.append(f"最后更新：{updated}")
    lines.append("")
    lines.append("*本仓库仅用于个人投资记录，不构成任何投资建议。*")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 渲染：HTML 看板
# --------------------------------------------------------------------------- #
HTML_CSS = """
:root {
  --bg: #f6f8fb;
  --card: #ffffff;
  --line: #e6ebf2;
  --text: #0f172a;
  --muted: #64748b;
  --blue: #2563eb;
  --up: #e11d48;
  --down: #059669;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 56px;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
.sub a { color: var(--blue); text-decoration: none; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 24px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; }
.card .k { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
.card .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.card .v.small { font-size: 17px; }
.up { color: var(--up); }
.down { color: var(--down); }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px 18px 10px; margin-bottom: 24px; }
.panel h2 { font-size: 15px; margin: 0 0 14px; font-weight: 600; }
.chart svg { width: 100%; height: auto; display: block; }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { padding: 10px 8px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
th:first-child, td:first-child { text-align: left; }
tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #fafcff; }
.note { color: var(--muted); font-size: 12px; line-height: 1.9; }
.note code { background: #eef2f7; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
"""


def render_html(config: dict, funds: list[dict], total: dict, timeline: list[dict]) -> str:
    cur = config.get("currency_symbol", "¥")
    updated = datetime.now(CST).strftime("%Y-%m-%d %H:%M (UTC+8)")
    latest_date = max((f["latest_nav_date"] for f in funds if f["latest_nav_date"]), default="-")
    svg = render_curve_svg(timeline)

    def cls(v: float) -> str:
        return "up" if v > 0 else ("down" if v < 0 else "")

    cards = [
        ("累计投入", f"{cur}{_fmt_money(total['invested'])}", ""),
        ("当前市值", f"{cur}{_fmt_money(total['market_value'])}", ""),
        ("累计盈亏", f"{cur}{_signed(total['profit'])}", cls(total["profit"])),
        ("累计收益率", _signed_pct(total["return_rate"]), cls(total["profit"])),
        ("最新净值日", latest_date, ""),
        ("累计交易日", f"{total['trading_days']} 天", ""),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v {c}">{v}</div></div>'
        for k, v, c in cards
    )

    fund_rows = "".join(
        f"<tr><td>{f['short_name']}</td><td>{f['code']}</td>"
        f"<td>{cur}{f['daily_amount']:.2f}</td><td>{f['invested']:,.2f}</td>"
        f"<td>{f['shares']:,.2f}</td><td>{f['avg_cost']:.4f}</td><td>{f['latest_nav']:.4f}</td>"
        f"<td>{f['market_value']:,.2f}</td>"
        f'<td class="{cls(f["profit"])}">{_signed(f["profit"])}</td>'
        f'<td class="{cls(f["profit"])}">{_signed_pct(f["return_rate"])}</td></tr>'
        for f in funds
    )

    recent_rows = "".join(
        f"<tr><td>{p['date']}</td><td>{p['invested']:,.2f}</td>"
        f"<td>{p['market_value']:,.2f}</td>"
        f'<td class="{cls(p["profit"])}">{_signed(p["profit"])}</td></tr>'
        for p in timeline[-15:][::-1]
    )

    rules = "".join(
        f"<li>{f['name']}（{f['code']}）：每个交易日 {cur}{f['daily_amount']:.2f}</li>" for f in funds
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{config['title']} · 看板</title>
<style>{HTML_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{config['title']}</h1>
  <div class="sub">起投 {config['start_date']} · 数据源：{config['source']} · 最后更新 {updated} ·
    <a href="{config['repo']}">GitHub 仓库</a></div>

  <div class="cards">{card_html}</div>

  <div class="panel">
    <h2>定投曲线</h2>
    <div class="chart">{svg}</div>
  </div>

  <div class="panel">
    <h2>持仓明细</h2>
    <table>
      <thead><tr><th>基金</th><th>代码</th><th>每日定投</th><th>投入本金</th><th>持有份额</th>
      <th>成本净值</th><th>最新净值</th><th>当前市值</th><th>累计盈亏</th><th>收益率</th></tr></thead>
      <tbody>{fund_rows}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>最近记录</h2>
    <table>
      <thead><tr><th>日期</th><th>累计投入</th><th>当前市值</th><th>累计盈亏</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>定投规则与说明</h2>
    <div class="note">
      <ul>
        {rules}
        <li>起投日：{config['start_date']}，无终止日期；非交易日不申购。</li>
      </ul>
      数据由 <code>scripts/update.py</code> 每日自动抓取并重算，GitHub Actions 提交更新。<br>
      本页面仅用于个人投资记录，不构成任何投资建议。
    </div>
  </div>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    start_date = config["start_date"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    log(f"开始更新：起始日 {start_date}")
    fund_results: list[dict] = []
    for fund_cfg in config["funds"]:
        _, navs = fetch_fund(fund_cfg["code"])
        fund_results.append(compute_fund(fund_cfg, navs, start_date))

    timeline = build_timeline(fund_results)

    total = {
        "daily_amount": round(sum(f["daily_amount"] for f in fund_results), 2),
        "trading_days": max((f["trading_days"] for f in fund_results), default=0),
        "invested": round(sum(f["invested"] for f in fund_results), 2),
        "shares": round(sum(f["shares"] for f in fund_results), 2),
        "market_value": round(sum(f["market_value"] for f in fund_results), 2),
    }
    total["profit"] = round(total["market_value"] - total["invested"], 2)
    total["return_rate"] = (
        round(total["profit"] / total["invested"], 6) if total["invested"] else 0.0
    )

    snapshot = {
        "updated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "start_date": start_date,
        "latest_nav_date": max((f["latest_nav_date"] for f in fund_results), default=""),
        "source": config["source"],
        "funds": [{k: v for k, v in f.items() if k != "daily"} for f in fund_results],
        "total": total,
    }

    (DATA_DIR / "portfolio.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (DATA_DIR / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (REPORTS_DIR / "curve.svg").write_text(render_curve_svg(timeline), encoding="utf-8")
    (REPORTS_DIR / "index.html").write_text(
        render_html(config, fund_results, total, timeline), encoding="utf-8"
    )
    (ROOT / "README.md").write_text(
        render_readme(config, fund_results, total, timeline), encoding="utf-8"
    )

    log(
        f"完成：投入 {total['invested']:.2f}，市值 {total['market_value']:.2f}，"
        f"盈亏 {total['profit']:+.2f}（{total['return_rate'] * 100:+.2f}%）"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"更新失败：{exc}")
        sys.exit(1)

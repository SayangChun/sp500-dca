#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标普500指数基金每日定投跟踪器

数据源
    1) 历史净值：天天基金 pingzhongdata 接口
       https://fund.eastmoney.com/pingzhongdata/{code}.js
    2) 申购状态：天天基金历史净值列表接口（主）/ F10 净值页（备）
       https://api.fund.eastmoney.com/f10/lsjz
       https://fundf10.eastmoney.com/jjjz_{code}.html

投资日期口径（按境内基金实际开放申购时间）
    - 只在「交易日」且「当日开放申购」时执行定投；
      非交易日、以及公告「暂停申购」的交易日一律不执行。
    - 申购时段：交易日 9:30–15:00（15:00 前提交视为当日 T 日）。
    - 确认规则：QDII 基金按 T 日单位净值确认，T+2 个交易日确认份额。
    - 因此每条记录同时标注「申购日(T)」与「份额确认日(T+2)」。

输出
    data/portfolio.json   当前持仓与统计快照
    data/timeline.json    逐日累计明细（仅含实际执行定投的交易日）
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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

CST = timezone(timedelta(hours=8))
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}&pageSize=50"
JJJZ_URL = "https://fundf10.eastmoney.com/jjjz_{code}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://fund.eastmoney.com/",
}

# 申购状态判定：命中以下关键词视为「不可申购」
SUSPEND_KEYWORDS = ("暂停申购", "暂停交易", "封闭")


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def round_half_up(value: float, digits: int = 2) -> float:
    """四舍五入（基金登记结算口径）。

    Python 内置 round() 采用银行家舍入（round-half-to-even），
    在 x.xx5 这类边界上会与基金公司的四舍五入结果不一致，故单独实现。
    """
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def is_suspended(status: str | None) -> bool:
    if not status:
        return False
    return any(kw in status for kw in SUSPEND_KEYWORDS)


# --------------------------------------------------------------------------- #
# 数据抓取
# --------------------------------------------------------------------------- #
def http_get(
    url: str,
    referer: str | None = None,
    retries: int = 4,
    timeout: int = 30,
    backoff: float = 2.0,
) -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"  请求失败（第 {attempt}/{retries} 次）：{exc}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"无法获取 {url}：{last_err}")


def fetch_fund_nav(code: str) -> tuple[str, list[tuple[str, float]]]:
    """返回 (基金名称, [(净值日期, 单位净值), ...] 升序)。"""
    text = http_get(PINGZHONG_URL.format(code=code))

    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not match:
        raise RuntimeError(f"未找到基金 {code} 的净值序列")

    name_match = re.search(r'var\s+fS_name\s*=\s*"([^"]*)"', text)
    name = name_match.group(1) if name_match else code

    navs: list[tuple[str, float]] = []
    for point in json.loads(match.group(1)):
        # 接口时间戳为「北京时间零点」对应的 UTC 毫秒值，须按 UTC+8 还原日期
        day = datetime.fromtimestamp(point["x"] / 1000, CST).strftime("%Y-%m-%d")
        navs.append((day, float(point["y"])))

    navs.sort(key=lambda item: item[0])
    log(f"  {code} {name}：共 {len(navs)} 条净值，最新 {navs[-1][0]} = {navs[-1][1]}")
    return name, navs


def _parse_status_json(text: str, start_date: str, out: dict[str, str]) -> int:
    data = json.loads(text)
    rows = (data.get("Data") or {}).get("LSJZList") or []
    for row in rows:
        day = row.get("FSRQ") or ""
        if day and day >= start_date:
            out[day] = (row.get("SGZT") or "").strip()
    return len(rows)


def _parse_status_html(html: str, start_date: str, out: dict[str, str]) -> int:
    count = 0
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) < 5 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        day = cells[0]
        if day < start_date:
            continue
        out[day] = cells[4]  # 列序：净值日期/单位净值/累计净值/日增长率/申购状态/赎回状态/分红送配
        count += 1
    return count


def fetch_status(code: str, start_date: str) -> tuple[dict[str, str], str]:
    """返回 ({日期: 申购状态}, 数据来源标识)。失败时返回 ({}, 'unavailable')。"""
    status: dict[str, str] = {}

    # 主源：历史净值列表 JSON 接口（含申购状态字段 SGZT）
    try:
        total = 0
        for page in range(1, 4):
            text = http_get(
                LSJZ_URL.format(code=code, page=page),
                referer="https://fundf10.eastmoney.com/",
                retries=2,
                timeout=15,
                backoff=1.0,
            )
            got = _parse_status_json(text, start_date, status)
            total += got
            if got < 50:
                break
            time.sleep(0.4)
        if status:
            log(f"  {code} 申购状态：来自列表接口，覆盖 {len(status)} 个交易日")
            return status, "eastmoney-lsjz"
    except Exception as exc:  # noqa: BLE001
        log(f"  {code} 列表接口不可用：{exc}")

    # 备源：F10 历史净值页（HTML 表格）
    try:
        html = http_get(
            JJJZ_URL.format(code=code),
            referer="https://fundf10.eastmoney.com/",
            retries=2,
            timeout=15,
            backoff=1.0,
        )
        got = _parse_status_html(html, start_date, status)
        if status:
            log(f"  {code} 申购状态：来自 F10 页面，覆盖 {len(status)} 个交易日")
            return status, "eastmoney-f10-html"
        log(f"  {code} F10 页面未解析到有效状态（{got} 行）")
    except Exception as exc:  # noqa: BLE001
        log(f"  {code} F10 页面不可用：{exc}")

    return {}, "unavailable"


# --------------------------------------------------------------------------- #
# 定投计算
# --------------------------------------------------------------------------- #
def compute_fund(
    fund_cfg: dict,
    navs: list[tuple[str, float]],
    start_date: str,
    status: dict[str, str],
    forced_suspended: set[str],
    confirm_lag: int,
    share_decimals: int,
) -> dict:
    amount = float(fund_cfg["daily_amount"])
    fee_rate = float(fund_cfg.get("purchase_fee_rate") or 0.0)
    all_days = [d for d, _ in navs]
    # 交易日历 = 已披露净值日 + 其后的工作日（仅用于推算 T+N 确认日）
    calendar = list(all_days)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    cursor = datetime.strptime(all_days[-1], "%Y-%m-%d") + timedelta(days=1)
    while cursor.strftime("%Y-%m-%d") <= today:
        if cursor.weekday() < 5:
            calendar.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    day_index = {d: i for i, d in enumerate(calendar)}

    shares = 0.0
    invested = 0.0
    daily: list[dict] = []
    skipped: list[dict] = []

    for day, nav in navs:
        if day < start_date:
            continue

        st = status.get(day, "")
        if day in forced_suspended or is_suspended(st):
            skipped.append(
                {
                    "date": day,
                    "nav": round(nav, 4),
                    "status": st or "暂停申购",
                    "reason": "基金当日暂停申购，定投未执行",
                }
            )
            continue

        # 前端收费：净申购金额 = 申购金额 ÷ (1 + 申购费率)，四舍五入保留 2 位小数
        net_amount = round_half_up(amount / (1 + fee_rate), 2)
        fee = round_half_up(amount - net_amount, 2)
        # 申购份额 = 净申购金额 ÷ T日基金份额净值，四舍五入保留 share_decimals 位小数
        bought = round_half_up(net_amount / nav, share_decimals)

        shares += bought
        invested += amount

        i = day_index[day]
        confirm_date = calendar[i + confirm_lag] if i + confirm_lag < len(calendar) else None

        daily.append(
            {
                "date": day,
                "nav": round(nav, 4),
                "amount": round_half_up(amount, 2),
                "fee": fee,
                "net_amount": net_amount,
                "shares": bought,
                "cum_shares": round_half_up(shares, share_decimals),
                "cum_invested": round_half_up(invested, 2),
                "market_value": round_half_up(shares * nav, 2),
                "confirm_date": confirm_date,
                "status": st,
            }
        )

    latest_nav = daily[-1]["nav"] if daily else 0.0
    market_value = shares * latest_nav
    profit = market_value - invested

    total_fee = round_half_up(sum(d["fee"] for d in daily), 2)

    # 可选：与用户 App 中的实际份额核对
    expected = fund_cfg.get("verify_shares")
    verify = None
    if expected is not None:
        diff = round_half_up(shares - float(expected), share_decimals)
        verify = {
            "expected": round_half_up(float(expected), share_decimals),
            "actual": round_half_up(shares, share_decimals),
            "diff": diff,
            "ok": abs(diff) <= 0.01,
        }

    return {
        "code": fund_cfg["code"],
        "name": fund_cfg["name"],
        "short_name": fund_cfg.get("short_name", fund_cfg["name"]),
        "share_class": fund_cfg.get("share_class", ""),
        "daily_amount": round_half_up(amount, 2),
        "purchase_fee_rate": fee_rate,
        "daily_limit": fund_cfg.get("daily_limit"),
        "trading_days": len(daily),
        "skipped_days": len(skipped),
        "shares": round_half_up(shares, share_decimals),
        "invested": round_half_up(invested, 2),
        "total_fee": total_fee,
        "avg_cost": round(invested / shares, 4) if shares else 0.0,
        "latest_nav": round(latest_nav, 4),
        "latest_nav_date": daily[-1]["date"] if daily else "",
        "market_value": round_half_up(market_value, 2),
        "profit": round_half_up(profit, 2),
        "return_rate": round(profit / invested, 6) if invested else 0.0,
        "verify": verify,
        "daily": daily,
        "skipped": skipped,
    }


def build_timeline(fund_results: list[dict]) -> list[dict]:
    """合并各基金为组合层面的逐日累计曲线（仅含实际执行定投的交易日）。"""
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
                "invested": round_half_up(invested, 2),
                "market_value": round_half_up(value, 2),
                "profit": round_half_up(value - invested, 2),
            }
        )
    return timeline


def aggregate_skipped(funds: list[dict]) -> list[dict]:
    """把各基金的「暂停申购」日按日期合并，便于展示。"""
    merged: dict[str, dict] = {}
    for f in funds:
        for s in f["skipped"]:
            item = merged.setdefault(
                s["date"],
                {"date": s["date"], "status": s["status"], "funds": [], "navs": {}},
            )
            item["funds"].append(f["short_name"])
            item["navs"][f["short_name"]] = s["nav"]
    return [merged[d] for d in sorted(merged)]


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
        "<defs>",
        '<linearGradient id="mvFill" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0%" stop-color="#3b82f6" stop-opacity="0.28"/>',
        '<stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>',
        "</linearGradient>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{pad_l}" y="24" font-size="15" font-weight="600" fill="#0f172a">'
        f"定投累计投入 vs 当前市值</text>",
        f'<line x1="{pad_l}" y1="40" x2="{pad_l+16}" y2="40" stroke="#94a3b8" '
        f'stroke-width="2.5" stroke-dasharray="5 4"/>',
        f'<text x="{pad_l+22}" y="44" font-size="12" fill="#64748b">累计投入</text>',
        f'<line x1="{pad_l+100}" y1="40" x2="{pad_l+116}" y2="40" stroke="#2563eb" stroke-width="2.5"/>',
        f'<text x="{pad_l+122}" y="44" font-size="12" fill="#64748b">当前市值</text>',
    ]

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

    parts.append(
        f'<circle cx="{x_at(n-1):.1f}" cy="{y_at(timeline[-1]["invested"]):.1f}" r="4" '
        f'fill="#ffffff" stroke="#94a3b8" stroke-width="2.5"/>'
    )
    parts.append(
        f'<circle cx="{x_at(n-1):.1f}" cy="{y_at(timeline[-1]["market_value"]):.1f}" r="4.5" '
        f'fill="#2563eb" stroke="#ffffff" stroke-width="2"/>'
    )

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


def render_readme(
    config: dict,
    funds: list[dict],
    total: dict,
    timeline: list[dict],
    status_sources: dict[str, str],
    updated: str,
    warnings: list[str],
) -> str:
    cur = config.get("currency_symbol", "¥")
    latest_date = max((f["latest_nav_date"] for f in funds if f["latest_nav_date"]), default="-")
    lag = config.get("confirm_lag", 2)
    share_decimals = int(config.get("share_decimals", 2))
    src_map = {
        "eastmoney-lsjz": "天天基金历史净值列表接口",
        "eastmoney-f10-html": "天天基金 F10 历史净值页",
        "unavailable": "未获取到（使用已知暂停申购日兜底）",
    }
    skipped_all = aggregate_skipped(funds)

    lines: list[str] = []
    lines.append(f"# {config['title']}")
    lines.append("")
    lines.append(
        "> 自动跟踪标普500指数基金（QDII）每日定投。数据取自天天基金披露的官方净值与申购状态，"
        "由 GitHub Actions 自动更新。"
    )
    lines.append("")
    lines.append(f"**仓库**：{config['repo']}")
    lines.append("")
    if warnings:
        lines.append("> [!WARNING]")
        lines.append("> **数据校验告警**")
        for w in warnings:
            lines.append(f"> - {w}")
        lines.append("")
    lines.append("## 当前状态")
    lines.append("")
    lines.append(f"- **开始定投**：{config['start_date']}（持续定投，无终止日期）")
    lines.append(f"- **最新净值日**：{latest_date}")
    lines.append(f"- **实际定投交易日**：{total['trading_days']} 天")
    if skipped_all:
        lines.append(f"- **因暂停申购跳过**：{len(skipped_all)} 天")
    lines.append(f"- **累计投入**：{cur}{_fmt_money(total['invested'])}")
    lines.append(f"- **当前市值**：{cur}{_fmt_money(total['market_value'])}")
    lines.append(
        f"- **累计盈亏**：{cur}{_signed(total['profit'])}"
        f"（{_signed_pct(total['return_rate'])}）"
    )
    lines.append("")
    lines.append("## 持仓明细")
    lines.append("")
    lines.append("| 基金 | 代码 | 每日定投 | 定投交易日 | 投入本金 | 持有份额 | 成本净值 | 最新净值 | 当前市值 | 累计盈亏 | 收益率 |")
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
    lines.append("| 申购日 (T) | 份额确认日 (T+2) | 单位净值 | 累计投入 | 当前市值 | 累计盈亏 |")
    lines.append("|:---|:---:|---:|---:|---:|---:|")
    for point in timeline[-10:][::-1]:
        fund0 = funds[0]["daily"]
        rec = next((r for r in fund0 if r["date"] == point["date"]), None)
        confirm = (rec or {}).get("confirm_date") or "待确认"
        nav = f"{(rec or {}).get('nav', 0):.4f}"
        lines.append(
            f"| {point['date']} | {confirm} | {nav} | "
            f"{cur}{_fmt_money(point['invested'])} | "
            f"{cur}{_fmt_money(point['market_value'])} | {cur}{_signed(point['profit'])} |"
        )
    lines.append("")
    if skipped_all:
        lines.append("## 非投资日说明")
        lines.append("")
        lines.append("以下交易日因基金**暂停申购**，当日定投未执行：")
        lines.append("")
        lines.append("| 日期 | 当日申购状态 | 涉及基金 | 当日单位净值 |")
        lines.append("|:---|:---|:---|---:|")
        for s in skipped_all:
            navs = " / ".join(f"{k} {v:.4f}" for k, v in s["navs"].items())
            lines.append(f"| {s['date']} | {s['status']} | {'、'.join(s['funds'])} | {navs} |")
        lines.append("")
    lines.append("## 定投规则（按境内基金实际开放申购口径）")
    lines.append("")
    lines.append("1. **投资日**：仅在该基金为交易日的当天执行；非交易日不申购。")
    lines.append("2. **申购状态**：仅当该交易日「开放申购」时执行；公告「暂停申购」的交易日一律不执行。")
    lines.append("3. **申购时段**：交易日 9:30–15:00；15:00 前提交的申请视为当日（T 日）。")
    lines.append(f"4. **确认规则**：QDII 基金按 **T 日单位净值**确认份额，**T+{lag} 个交易日**确认。")
    lines.append(
        f"5. **份额计算**：净申购金额 = 定投金额 ÷ (1 + 申购费率)；"
        f"份额 = 净申购金额 ÷ T 日单位净值，四舍五入保留 **{share_decimals} 位小数**。"
    )
    lines.append("")
    for f in funds:
        limit = f.get("daily_limit")
        limit_txt = f"，单日累计购买上限 {cur}{limit:.0f}" if limit else ""
        fee = f.get("purchase_fee_rate") or 0.0
        fee_txt = "，免申购费" if not fee else f"，申购费率 {fee * 100:g}%"
        lines.append(
            f"- {f['name']}（{f['code']}）：每个可申购交易日 {cur}{f['daily_amount']:.2f}"
            f"{fee_txt}{limit_txt}"
        )
    lines.append(f"- 起投日：{config['start_date']}，无终止日期。")
    lines.append("")
    lines.append("## 数据与自动化")
    lines.append("")
    lines.append(f"- 净值数据源：{config['source']}（{config['source_url']}）")
    for f in funds:
        lines.append(f"  - {f['code']} 申购状态：{src_map.get(status_sources.get(f['code'], ''), '—')}")
    lines.append("- 更新方式：GitHub Actions 定时任务，自动抓取净值与申购状态、重算持仓并提交结果。")
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
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; background: #fff1f2; color: #be123c; border: 1px solid #fecdd3; }
.note { color: var(--muted); font-size: 12px; line-height: 1.9; }
.note code { background: #eef2f7; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
"""


def render_html(
    config: dict,
    funds: list[dict],
    total: dict,
    timeline: list[dict],
    status_sources: dict[str, str],
    updated: str,
    warnings: list[str],
) -> str:
    cur = config.get("currency_symbol", "¥")
    latest_date = max((f["latest_nav_date"] for f in funds if f["latest_nav_date"]), default="-")
    lag = config.get("confirm_lag", 2)
    share_decimals = int(config.get("share_decimals", 2))
    svg = render_curve_svg(timeline)
    skipped_all = aggregate_skipped(funds)

    def cls(v: float) -> str:
        return "up" if v > 0 else ("down" if v < 0 else "")

    cards = [
        ("累计投入", f"{cur}{_fmt_money(total['invested'])}", ""),
        ("当前市值", f"{cur}{_fmt_money(total['market_value'])}", ""),
        ("累计盈亏", f"{cur}{_signed(total['profit'])}", cls(total["profit"])),
        ("累计收益率", _signed_pct(total["return_rate"]), cls(total["profit"])),
        ("最新净值日", latest_date, ""),
        ("定投交易日", f"{total['trading_days']} 天", ""),
    ]
    if skipped_all:
        cards.append(("暂停申购跳过", f"{len(skipped_all)} 天", ""))
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v {c}">{v}</div></div>'
        for k, v, c in cards
    )

    warning_panel = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warning_panel = (
            '<div class="panel" style="border-color:#fecdd3;background:#fff1f2">'
            '<h2 style="color:#be123c">数据校验告警</h2>'
            f'<div class="note" style="color:#9f1239"><ul>{items}</ul></div></div>'
        )

    fund_rows = "".join(
        f"<tr><td>{f['short_name']}</td><td>{f['code']}</td>"
        f"<td>{cur}{f['daily_amount']:.2f}</td><td>{f['trading_days']}</td><td>{f['invested']:,.2f}</td>"
        f"<td>{f['shares']:,.2f}</td><td>{f['avg_cost']:.4f}</td><td>{f['latest_nav']:.4f}</td>"
        f"<td>{f['market_value']:,.2f}</td>"
        f'<td class="{cls(f["profit"])}">{_signed(f["profit"])}</td>'
        f'<td class="{cls(f["profit"])}">{_signed_pct(f["return_rate"])}</td></tr>'
        for f in funds
    )

    fund0 = funds[0]["daily"]
    recent_rows = ""
    for p in timeline[-15:][::-1]:
        rec = next((r for r in fund0 if r["date"] == p["date"]), None)
        confirm = (rec or {}).get("confirm_date") or "待确认"
        nav = f"{(rec or {}).get('nav', 0):.4f}"
        recent_rows += (
            f"<tr><td>{p['date']}</td><td>{confirm}</td><td>{nav}</td>"
            f"<td>{p['invested']:,.2f}</td><td>{p['market_value']:,.2f}</td>"
            f'<td class="{cls(p["profit"])}">{_signed(p["profit"])}</td></tr>'
        )

    skipped_panel = ""
    if skipped_all:
        rows = "".join(
            f"<tr><td>{s['date']}</td>"
            f'<td><span class="tag">{s["status"]}</span></td>'
            f"<td>{'、'.join(s['funds'])}</td>"
            f"<td>{' / '.join(f'{k} {v:.4f}' for k, v in s['navs'].items())}</td></tr>"
            for s in skipped_all
        )
        skipped_panel = f"""
  <div class="panel">
    <h2>非投资日说明（暂停申购，定投未执行）</h2>
    <table>
      <thead><tr><th>日期</th><th>当日申购状态</th><th>涉及基金</th><th>当日单位净值</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>"""

    rules = "".join(
        f"<li>{f['name']}（{f['code']}）：每个可申购交易日 {cur}{f['daily_amount']:.2f}"
        + ("，免申购费" if not (f.get("purchase_fee_rate") or 0) else f"，申购费率 {f['purchase_fee_rate'] * 100:g}%")
        + (f"，单日累计购买上限 {cur}{f['daily_limit']:.0f}" if f.get("daily_limit") else "")
        + "</li>"
        for f in funds
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
{warning_panel}

  <div class="panel">
    <h2>定投曲线</h2>
    <div class="chart">{svg}</div>
  </div>

  <div class="panel">
    <h2>持仓明细</h2>
    <table>
      <thead><tr><th>基金</th><th>代码</th><th>每日定投</th><th>定投交易日</th><th>投入本金</th>
      <th>持有份额</th><th>成本净值</th><th>最新净值</th><th>当前市值</th><th>累计盈亏</th><th>收益率</th></tr></thead>
      <tbody>{fund_rows}</tbody>
    </table>
  </div>

  <div class="panel">
    <h2>最近记录</h2>
    <table>
      <thead><tr><th>申购日 (T)</th><th>份额确认日 (T+{lag})</th><th>单位净值</th>
      <th>累计投入</th><th>当前市值</th><th>累计盈亏</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table>
  </div>
{skipped_panel}
  <div class="panel">
    <h2>定投规则（按境内基金实际开放申购口径）</h2>
    <div class="note">
      <ol>
        <li>投资日：仅在该基金为交易日的当天执行；非交易日不申购。</li>
        <li>申购状态：仅当该交易日「开放申购」时执行；公告「暂停申购」的交易日一律不执行。</li>
        <li>申购时段：交易日 9:30–15:00；15:00 前提交的申请视为当日（T 日）。</li>
        <li>确认规则：QDII 基金按 <b>T 日单位净值</b>确认份额，<b>T+{lag} 个交易日</b>确认。</li>
        <li>份额计算：净申购金额 = 定投金额 ÷ (1 + 申购费率)；份额 = 净申购金额 ÷ T 日单位净值，四舍五入保留 <b>{share_decimals} 位小数</b>。</li>
      </ol>
      <ul>
        {rules}
        <li>起投日：{config['start_date']}，无终止日期。</li>
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
    confirm_lag = int(config.get("confirm_lag", 2))
    share_decimals = int(config.get("share_decimals", 2))
    forced_suspended = set(config.get("suspended_dates") or [])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    log(f"开始更新：起始日 {start_date}，份额确认 T+{confirm_lag}，份额保留 {share_decimals} 位小数")

    fund_results: list[dict] = []
    status_sources: dict[str, str] = {}
    warnings: list[str] = []
    for fund_cfg in config["funds"]:
        code = fund_cfg["code"]
        amt = float(fund_cfg["daily_amount"])
        limit = fund_cfg.get("daily_limit")
        floor = fund_cfg.get("min_amount")
        if limit is not None and amt > float(limit):
            warnings.append(f"{code} 每日定投 {amt:.2f} 超过单日累计购买上限 {float(limit):.2f}")
        if floor is not None and amt < float(floor):
            warnings.append(f"{code} 每日定投 {amt:.2f} 低于起投金额 {float(floor):.2f}")

        _, navs = fetch_fund_nav(code)
        status, src = fetch_status(code, start_date)
        status_sources[code] = src
        result = compute_fund(
            fund_cfg, navs, start_date, status, forced_suspended, confirm_lag, share_decimals
        )
        if result["skipped"]:
            days = ", ".join(s["date"] for s in result["skipped"])
            log(f"  {code} 跳过 {len(result['skipped'])} 个暂停申购日：{days}")
        if result["verify"]:
            v = result["verify"]
            log(
                f"  {code} 份额核对：{'✅ 一致' if v['ok'] else '❌ 不一致'}"
                f"（脚本 {v['actual']} vs App {v['expected']}，差 {v['diff']:+}）"
            )
            if not v["ok"]:
                warnings.append(
                    f"{code} 计算份额 {v['actual']} 与 App 实际 {v['expected']} 不符"
                    f"（差 {v['diff']:+}）"
                )
        fund_results.append(result)

    for w in warnings:
        log(f"⚠️ 校验告警：{w}")

    timeline = build_timeline(fund_results)

    total = {
        "daily_amount": round_half_up(sum(f["daily_amount"] for f in fund_results), 2),
        "trading_days": max((f["trading_days"] for f in fund_results), default=0),
        "skipped_days": len(aggregate_skipped(fund_results)),
        "invested": round_half_up(sum(f["invested"] for f in fund_results), 2),
        "shares": round_half_up(sum(f["shares"] for f in fund_results), share_decimals),
        "total_fee": round_half_up(sum(f["total_fee"] for f in fund_results), 2),
        # 组合市值按未取整数值求和，避免逐只取整后再相加产生 0.01 偏差
        "market_value": round_half_up(
            sum(f["shares"] * f["latest_nav"] for f in fund_results), 2
        ),
    }
    total["profit"] = round_half_up(total["market_value"] - total["invested"], 2)
    total["return_rate"] = (
        round(total["profit"] / total["invested"], 6) if total["invested"] else 0.0
    )

    core = {
        "start_date": start_date,
        "confirm_lag": confirm_lag,
        "share_decimals": share_decimals,
        "latest_nav_date": max((f["latest_nav_date"] for f in fund_results), default=""),
        "source": config["source"],
        "status_source": status_sources,
        "warnings": warnings,
        "skipped_dates": aggregate_skipped(fund_results),
        "funds": [{k: v for k, v in f.items() if k != "daily"} for f in fund_results],
        "total": total,
    }

    # 数据未变化时沿用上次的时间戳，使输出逐字节一致，避免无意义提交
    portfolio_path = DATA_DIR / "portfolio.json"
    updated_at = datetime.now(CST).isoformat(timespec="seconds")
    if portfolio_path.exists():
        try:
            prev = json.loads(portfolio_path.read_text(encoding="utf-8"))
            prev_core = {k: v for k, v in prev.items() if k != "updated_at"}
            if prev_core == core and prev.get("updated_at"):
                updated_at = prev["updated_at"]
                log("数据无变化，沿用上次更新时间戳")
        except Exception as exc:  # noqa: BLE001
            log(f"  读取旧快照失败（忽略）：{exc}")

    snapshot = {"updated_at": updated_at, **core}
    display_updated = datetime.fromisoformat(updated_at).strftime("%Y-%m-%d %H:%M (UTC+8)")

    portfolio_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (DATA_DIR / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (REPORTS_DIR / "curve.svg").write_text(render_curve_svg(timeline), encoding="utf-8")
    (REPORTS_DIR / "index.html").write_text(
        render_html(
            config, fund_results, total, timeline, status_sources, display_updated, warnings
        ),
        encoding="utf-8",
    )
    (ROOT / "README.md").write_text(
        render_readme(
            config, fund_results, total, timeline, status_sources, display_updated, warnings
        ),
        encoding="utf-8",
    )

    log(
        f"完成：定投 {total['trading_days']} 个交易日，跳过 {total['skipped_days']} 天；"
        f"投入 {total['invested']:.2f}，市值 {total['market_value']:.2f}，"
        f"盈亏 {total['profit']:+.2f}（{total['return_rate'] * 100:+.2f}%）"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"更新失败：{exc}")
        sys.exit(1)

"""把当前快照收成看板结论。以现在的正式信号为准，不用历史任务顶标题。"""

from __future__ import annotations

from typing import Any


def _action_zh(action: str | None) -> str:
    return {
        "BUY": "买入",
        "SELL": "卖出",
        "HOLD": "持有",
        "ABANDON": "放弃",
        "WATCH": "等待确认",
    }.get(str(action or ""), str(action or "未知"))


def _side_zh(side: str | None) -> str:
    return {"BUY": "做多", "SELL": "做空"}.get(str(side or ""), str(side or ""))


def _market_zh(market: Any) -> str:
    return {"CRYPTO": "Crypto", "A_SHARE": "A股"}.get(str(market or ""), str(market or ""))


def _notes(row: dict) -> str:
    bits = [str(item) for item in (row.get("why_source") or []) if item]
    for key in ("why", "why_not", "skip_reason"):
        if row.get(key):
            bits.append(str(row[key]))
    return " ".join(bits)


def _waiting(row: dict) -> bool:
    text = _notes(row).lower()
    return "waiting" in text or "confirm" in text or "确认" in text


def _strength(row: dict) -> float:
    try:
        return float(row.get("strength") or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any, suffix: str) -> str:
    if value in (None, ""):
        return "—"
    text = str(value)
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:2]}"
    return f"{text} {suffix}"


def _latest_by_symbol(decisions: list[dict], market: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in decisions:
        if row.get("market") != market:
            continue
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in latest:
            latest[symbol] = row
    return latest


def build_today(
    *,
    crypto_health: dict | None,
    ashare_health: dict | None,
    crypto_account: dict | None,
    ashare_account: dict | None,
    crypto_signals: list[dict],
    ashare_signals: list[dict],
    crypto_watch: dict,
    ashare_watch: dict,
    decisions: list[dict],
    attention: list[dict] | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        decisions,
        key=lambda row: str(row.get("updated_at") or ""),
        reverse=True,
    )
    crypto_now = _current_crypto(crypto_signals, _latest_by_symbol(ordered, "CRYPTO"))
    ashare_now = _current_ashare(
        ashare_signals,
        _latest_by_symbol(ordered, "A_SHARE"),
        str((ashare_health or {}).get("market_session") or "UNKNOWN"),
    )
    screens = [
        row
        for row in (ashare_watch.get("screen_results") or [])
        if str(row.get("kind") or "").upper() == "SCREEN_RESULT"
    ]
    rejects = list(crypto_watch.get("rejected_candidates") or [])
    headline = _headline(crypto_now, ashare_now)
    crypto_story = _crypto_story(
        account=crypto_account,
        current=crypto_now,
        rejects=rejects,
    )
    ashare_story = _ashare_story(
        account=ashare_account,
        current=ashare_now,
        screens=screens,
        session=str((ashare_health or {}).get("market_session") or "UNKNOWN"),
        health=ashare_health,
    )
    focus = [
        {**row, "action": row.get("board_action")}
        for row in crypto_now + ashare_now
        if row.get("board_action") in {"BUY", "SELL", "WATCH"}
    ][:5]
    abandons = [
        {**row, "action": row.get("board_action")}
        for row in crypto_now + ashare_now
        if row.get("board_action") == "ABANDON"
    ][:5]
    return {
        "headline": headline,
        "stories": [crypto_story, ashare_story],
        "focus": focus,
        "abandons": abandons,
        "watching": [row for row in crypto_now if row.get("board_action") == "WATCH"][:6],
        "screens": screens[:6],
        "counts": {
            "crypto_signals": len(crypto_signals),
            "ashare_signals": len(ashare_signals),
            "screens": len(screens),
            "rejects": len(rejects),
            "execute": sum(1 for row in crypto_now + ashare_now if row.get("board_action") in {"BUY", "SELL"}),
            "watch": sum(1 for row in crypto_now if row.get("board_action") == "WATCH"),
            "abandon": len(abandons),
        },
        "attention": (attention or [])[:5],
        "disclaimer": "仅模拟，不会下单。筛选结果不是正式信号。",
    }


def _current_crypto(signals: list[dict], latest: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for signal in signals:
        symbol = str(signal.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        decision = latest.get(symbol) or {}
        waiting = _waiting(signal) or _waiting(decision)
        skip = str(decision.get("skip_reason") or "")
        if waiting:
            action = "WATCH"
        elif skip:
            action = "ABANDON"
        else:
            action = str(decision.get("action") or signal.get("side") or "HOLD")
        rows.append(
            {
                **signal,
                **{k: v for k, v in decision.items() if v not in (None, "")},
                "market": "CRYPTO",
                "symbol": symbol,
                "board_action": action,
                "side": signal.get("side"),
                "strength": signal.get("strength"),
                "note": _notes(signal) or _notes(decision),
            }
        )
    rows.sort(key=_strength, reverse=True)
    return rows


def _current_ashare(signals: list[dict], latest: dict[str, dict], session: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    closed = session == "CLOSED"
    for signal in signals:
        symbol = str(signal.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        decision = latest.get(symbol) or {}
        skip = str(decision.get("skip_reason") or "")
        if closed:
            action = "ABANDON"
            skip = skip or "MARKET_CLOSED"
        elif skip:
            action = "ABANDON"
        else:
            action = str(decision.get("action") or signal.get("side") or "HOLD")
        rows.append(
            {
                **signal,
                **{k: v for k, v in decision.items() if v not in (None, "")},
                "market": "A_SHARE",
                "symbol": symbol,
                "board_action": action,
                "skip_reason": skip or None,
                "side": signal.get("side"),
                "strength": signal.get("strength"),
                "note": _notes(signal) or _notes(decision),
            }
        )
    rows.sort(key=_strength, reverse=True)
    return rows


def _headline(crypto_now: list[dict], ashare_now: list[dict]) -> str:
    execute = [row for row in crypto_now + ashare_now if row.get("board_action") in {"BUY", "SELL"}]
    watching = [row for row in crypto_now if row.get("board_action") == "WATCH"]
    closed = [
        row
        for row in ashare_now
        if row.get("board_action") == "ABANDON" and row.get("skip_reason") == "MARKET_CLOSED"
    ]
    if execute:
        top = execute[0]
        return (
            f"今天最该看：{_market_zh(top.get('market'))} "
            f"{_action_zh(top.get('board_action'))} {top.get('symbol')}"
        )
    if watching:
        top = watching[0]
        return (
            f"今天最该看：Crypto 等待确认{_side_zh(top.get('side'))} {top.get('symbol')}"
        )
    if closed:
        return f"A股已闭市，{len(closed)} 条正式决策今晚不执行"
    if crypto_now or ashare_now:
        return "已接到正式信号，但当前都不执行"
    return "两边量化系统此刻没有正式可执行信号"


def _crypto_story(*, account: dict | None, current: list[dict], rejects: list[dict]) -> dict[str, Any]:
    watching = [row for row in current if row.get("board_action") == "WATCH"]
    execute = [row for row in current if row.get("board_action") in {"BUY", "SELL"}]
    points: list[str] = [
        f"权益 {_money((account or {}).get('equity'), 'USDT')}，现金 {_money((account or {}).get('cash'), 'USDT')}。"
    ]
    if execute:
        title = f"Crypto 建议{_action_zh(execute[0].get('board_action'))} {execute[0].get('symbol')}"
        points.append(
            "当前建议："
            + "；".join(
                f"{_action_zh(row.get('board_action'))} {row.get('symbol')}"
                for row in execute[:3]
            )
            + "。"
        )
    elif watching:
        title = f"Crypto 在等确认，最强是{_side_zh(watching[0].get('side'))} {watching[0].get('symbol')}"
        points.append(
            "还没走完确认："
            + "、".join(
                f"{_side_zh(row.get('side'))} {row.get('symbol')}"
                for row in watching[:3]
            )
            + "。这不是已经可以下的单。"
        )
    else:
        title = "Crypto 此刻没有正式可执行信号"
        points.append("6celue 没有 pending/executed 的正式多空单。")
    if rejects:
        names = "、".join(str(row.get("symbol")) for row in rejects[:3])
        points.append(f"另外挡掉了 {names} 等，不会当成买入建议。")
    return {"market": "CRYPTO", "title": title, "points": points}


def _ashare_story(
    *,
    account: dict | None,
    current: list[dict],
    screens: list[dict],
    session: str,
    health: dict | None,
) -> dict[str, Any]:
    closed = session == "CLOSED"
    execute = [row for row in current if row.get("board_action") in {"BUY", "SELL"}]
    abandons = [row for row in current if row.get("board_action") == "ABANDON"]
    points: list[str] = [
        f"权益 {_money((account or {}).get('equity'), '元')}，现金 {_money((account or {}).get('cash'), '元')}。"
        + ("现在闭市。" if closed else "现在开市。")
    ]
    if execute:
        title = f"A股建议{_action_zh(execute[0].get('board_action'))} {execute[0].get('symbol')}"
        points.append(
            "正式决策："
            + "、".join(
                f"{_action_zh(row.get('board_action'))} {row.get('symbol')}"
                for row in execute[:4]
            )
            + "。"
        )
    elif abandons and closed:
        buys = [row.get("symbol") for row in current if row.get("side") == "BUY"]
        sells = [row.get("symbol") for row in current if row.get("side") == "SELL"]
        title = f"A股闭市，{len(abandons)} 条正式决策不执行"
        detail = []
        if buys:
            detail.append("想买 " + "、".join(str(item) for item in buys[:3]))
        if sells:
            detail.append("想卖 " + "、".join(str(item) for item in sells[:3]))
        if detail:
            points.append("、".join(detail) + "。闭市所以放弃，不是数据坏了。")
        else:
            points.append("正式信号已放弃，原因是闭市。")
    elif abandons:
        title = f"A股 {len(abandons)} 条正式信号已放弃"
        points.append(f"原因：{abandons[0].get('skip_reason') or '规则拦截'}。")
    elif current:
        title = "A股已有正式信号，等待记录决策"
        points.append(f"接到 {len(current)} 条政策决策。")
    else:
        title = "A股此刻没有正式政策决策"
        points.append("cockpit 没有可执行的 buy/add/reduce/exit。")
    if screens:
        names = "、".join(str(row.get("symbol")) for row in screens[:3])
        points.append(f"筛选还看到 {names} 等，只是选股，不是下单信号。")
    return {"market": "A_SHARE", "title": title, "points": points}

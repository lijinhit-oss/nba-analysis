"""
NBA Daily Analysis Report Generator
Fetches Pinnacle odds, Polymarket prices, ESPN data and generates HTML report.
"""

import os
import json
import requests
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
POLYMARKET_API_URL = "https://gamma-api.polymarket.com/events"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"

TEAM_NAMES = {
    "Atlanta Hawks": "老鹰",
    "Boston Celtics": "凯尔特人",
    "Brooklyn Nets": "篮网",
    "Charlotte Hornets": "黄蜂",
    "Chicago Bulls": "公牛",
    "Cleveland Cavaliers": "骑士",
    "Dallas Mavericks": "独行侠",
    "Denver Nuggets": "掘金",
    "Detroit Pistons": "活塞",
    "Golden State Warriors": "勇士",
    "Houston Rockets": "火箭",
    "Indiana Pacers": "步行者",
    "LA Clippers": "快船",
    "Los Angeles Lakers": "湖人",
    "Memphis Grizzlies": "灰熊",
    "Miami Heat": "热火",
    "Milwaukee Bucks": "雄鹿",
    "Minnesota Timberwolves": "森林狼",
    "New Orleans Pelicans": "鹈鹕",
    "New York Knicks": "尼克斯",
    "Oklahoma City Thunder": "雷霆",
    "Orlando Magic": "魔术",
    "Philadelphia 76ers": "76人",
    "Phoenix Suns": "太阳",
    "Portland Trail Blazers": "开拓者",
    "Sacramento Kings": "国王",
    "San Antonio Spurs": "马刺",
    "Toronto Raptors": "猛龙",
    "Utah Jazz": "爵士",
    "Washington Wizards": "奇才",
}

BEIJING_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def odds_to_prob(odds: float) -> float:
    """Convert American odds to implied probability."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def remove_vig(home_odds: float, away_odds: float):
    """Return vig-removed (true) probabilities for home and away teams."""
    hp = odds_to_prob(home_odds)
    ap = odds_to_prob(away_odds)
    total = hp + ap
    return hp / total, ap / total


def get_signal(gap: float) -> str:
    gap = abs(gap)
    if gap > 0.10:
        return "🔴 重大偏差"
    elif gap > 0.05:
        return "🟡 中等偏差"
    else:
        return "🟢 基本一致"


def get_signal_class(gap: float) -> str:
    gap = abs(gap)
    if gap > 0.10:
        return "signal-red"
    elif gap > 0.05:
        return "signal-yellow"
    else:
        return "signal-green"


def get_priority(gap: float) -> str:
    gap = abs(gap)
    if gap > 0.10:
        return "★★★"
    elif gap > 0.05:
        return "★★☆"
    else:
        return "★☆☆"


def get_priority_sort_key(gap: float) -> int:
    gap = abs(gap)
    if gap > 0.10:
        return 3
    elif gap > 0.05:
        return 2
    else:
        return 1


def get_strategy(home_prob: float, away_prob: float) -> str:
    gap = abs(home_prob - away_prob)
    if gap < 0.15:
        return "策略一"
    else:
        return "策略三"


def get_strategy_desc(strategy: str, home_team_cn: str, away_team_cn: str,
                      home_prob: float, pm_home_prob: float) -> str:
    if strategy == "策略一":
        return (
            f"两队实力相近（Pinnacle差值<15%），比赛结果不确定性高。"
            f"重点关注盘口变化与市场情绪，若Polymarket与Pinnacle偏差扩大可考虑介入。"
        )
    else:
        fav = home_team_cn if home_prob > 0.5 else away_team_cn
        underdog = away_team_cn if home_prob > 0.5 else home_team_cn
        return (
            f"两队实力差距明显（Pinnacle差值≥15%），{fav}为强烈大热门。"
            f"若Polymarket低估{fav}胜率，存在冷门价值博弈机会；"
            f"若高估{underdog}，需警惕跟风风险。"
        )


def get_motivation(conf_rank: int, wins: int, losses: int):
    """Return (level_str, reason_str) for a team's current motivation."""
    if conf_rank <= 6:
        return "强", "争种子排位"
    elif conf_rank <= 10:
        return "强", "争季后赛/附加赛"
    elif losses > 50:
        return "弱", "赛季失败，摆烂嫌疑"
    else:
        return "中", "赛季边缘"


def fmt_prob(p: float) -> str:
    return f"{p * 100:.1f}%"


def fmt_gap(gap: float) -> str:
    sign = "+" if gap >= 0 else "-"
    return f"{sign}{abs(gap) * 100:.1f}%"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_pinnacle_odds() -> list:
    """Fetch Pinnacle h2h odds from TheOddsAPI. Returns list of game dicts."""
    if not ODDS_API_KEY:
        print("[WARN] ODDS_API_KEY not set; skipping Pinnacle fetch.")
        return []
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "bookmakers": "pinnacle",
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(ODDS_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        print(f"[INFO] Pinnacle: fetched {len(data)} games.")
        return data
    except Exception as e:
        print(f"[ERROR] Pinnacle fetch failed: {e}")
        return []


def fetch_polymarket_nba() -> list:
    """
    Fetch Polymarket NBA game markets from gamma-api events endpoint.
    Returns list of dicts with team names and win probabilities.
    """
    all_events = []
    offset = 0
    limit = 100

    try:
        while offset <= 300:
            params = {
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "tag_slug": "nba",
                "order": "startDate",
                "ascending": "true",
            }
            resp = requests.get(POLYMARKET_API_URL, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
    except Exception as e:
        print(f"[ERROR] Polymarket fetch failed: {e}")
        return []

    nba_markets = []
    for event in all_events:
        title = event.get("title", "") or ""
        # Skip non-game events (Champion, MVP, etc.)
        if "vs." not in title and " vs " not in title:
            continue

        markets = event.get("markets", [])
        for m in markets:
            question = m.get("question", "") or ""
            # Only moneyline markets (skip O/U, Spread, etc.)
            if any(skip in question for skip in ["O/U", "Over", "Under", "1H", "Half", "Quarter", "Spread", "ATS", "+"]):
                continue

            try:
                outcome_prices_raw = m.get("outcomePrices", "[]")
                outcomes_raw = m.get("outcomes", "[]")
                if isinstance(outcome_prices_raw, str):
                    prices = json.loads(outcome_prices_raw)
                else:
                    prices = list(outcome_prices_raw)
                if isinstance(outcomes_raw, str):
                    outcomes = json.loads(outcomes_raw)
                else:
                    outcomes = list(outcomes_raw)
                prices = [float(p) for p in prices]
                if len(prices) < 2:
                    continue
                m["_prices"] = prices
                m["_outcomes"] = outcomes
                m["_event_title"] = title
                nba_markets.append(m)
                break  # one moneyline market per event is enough
            except Exception:
                continue

    print(f"[INFO] Polymarket: found {len(nba_markets)} NBA game markets.")
    return nba_markets


def fetch_espn_scoreboard() -> dict:
    """Fetch ESPN NBA scoreboard for today. Returns raw JSON or {}."""
    try:
        resp = requests.get(ESPN_SCOREBOARD_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        print(f"[INFO] ESPN scoreboard fetched.")
        return data
    except Exception as e:
        print(f"[ERROR] ESPN scoreboard fetch failed: {e}")
        return {}


def fetch_espn_standings() -> dict:
    """Fetch ESPN NBA standings. Returns dict keyed by team display name."""
    standings_map = {}
    try:
        resp = requests.get(ESPN_STANDINGS_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        children = data.get("children", [])
        conf_rank_tracker = {}  # conference -> count
        for conf_group in children:
            conf_name = conf_group.get("name", "")
            conf_abbr = "E" if "East" in conf_name else "W"
            entries = conf_group.get("standings", {}).get("entries", [])
            for rank_idx, entry in enumerate(entries):
                team_info = entry.get("team", {})
                team_name = team_info.get("displayName", "")
                stats_list = entry.get("stats", [])
                wins = losses = 0
                for stat in stats_list:
                    if stat.get("name") == "wins":
                        wins = int(stat.get("value", 0))
                    elif stat.get("name") == "losses":
                        losses = int(stat.get("value", 0))
                standings_map[team_name] = {
                    "conf": conf_abbr,
                    "conf_rank": rank_idx + 1,
                    "wins": wins,
                    "losses": losses,
                }
        print(f"[INFO] ESPN standings: {len(standings_map)} teams.")
    except Exception as e:
        print(f"[ERROR] ESPN standings fetch failed: {e}")
    return standings_map


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def find_polymarket_prob(pm_markets: list, home_team: str, away_team: str):
    """
    Try to find a Polymarket market for this matchup.
    Returns (home_win_prob, market_question) or (None, None).
    """
    home_short = home_team.split()[-1]  # e.g. "Lakers"
    away_short = away_team.split()[-1]

    for m in pm_markets:
        title = m.get("_event_title", "") or m.get("question", "")
        home_hit = home_short.lower() in title.lower() or home_team.lower() in title.lower()
        away_hit = away_short.lower() in title.lower() or away_team.lower() in title.lower()
        if not (home_hit and away_hit):
            continue

        prices = m["_prices"]       # [team1_prob, team2_prob]
        outcomes = m["_outcomes"]   # ["Team1", "Team2"]

        # Find which index corresponds to home team
        home_idx = 0
        for i, outcome in enumerate(outcomes):
            if home_short.lower() in outcome.lower() or home_team.lower() in outcome.lower():
                home_idx = i
                break

        home_prob = prices[home_idx]
        return home_prob, title

    return None, None


def parse_espn_games(scoreboard: dict, standings_map: dict) -> list:
    """
    Extract structured game info from ESPN scoreboard JSON.
    Returns list of game dicts.
    """
    games = []
    events = scoreboard.get("events", [])
    for event in events:
        try:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home_comp = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away_comp = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

            home_name = home_comp.get("team", {}).get("displayName", "")
            away_name = away_comp.get("team", {}).get("displayName", "")

            # Game time in Beijing
            date_str = event.get("date", "")
            game_time_bj = "N/A"
            if date_str:
                try:
                    dt_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    dt_bj = dt_utc.astimezone(BEIJING_TZ)
                    game_time_bj = dt_bj.strftime("%m/%d %H:%M")
                except Exception:
                    pass

            home_stand = standings_map.get(home_name, {})
            away_stand = standings_map.get(away_name, {})

            games.append({
                "home": home_name,
                "away": away_name,
                "game_time_bj": game_time_bj,
                "home_wins": home_stand.get("wins", "N/A"),
                "home_losses": home_stand.get("losses", "N/A"),
                "home_conf": home_stand.get("conf", "?"),
                "home_conf_rank": home_stand.get("conf_rank", 99),
                "away_wins": away_stand.get("wins", "N/A"),
                "away_losses": away_stand.get("losses", "N/A"),
                "away_conf": away_stand.get("conf", "?"),
                "away_conf_rank": away_stand.get("conf_rank", 99),
            })
        except Exception as e:
            print(f"[WARN] parse_espn_games error: {e}")
    return games


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def build_analysis_rows(espn_games: list, pinnacle_data: list, pm_markets: list) -> list:
    """
    For each ESPN game, find Pinnacle odds and Polymarket price,
    compute signals, and return a sorted list of row dicts.
    """
    # Build Pinnacle lookup: (home_team, away_team) -> (home_odds, away_odds)
    pinnacle_lookup = {}
    for game in pinnacle_data:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        bookmakers = game.get("bookmakers", [])
        for bm in bookmakers:
            if bm.get("key") != "pinnacle":
                continue
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                home_odds = away_odds = None
                for o in outcomes:
                    if o.get("name") == home_team:
                        home_odds = o.get("price")
                    elif o.get("name") == away_team:
                        away_odds = o.get("price")
                if home_odds is not None and away_odds is not None:
                    pinnacle_lookup[(home_team, away_team)] = (home_odds, away_odds)

    rows = []
    for g in espn_games:
        home = g["home"]
        away = g["away"]
        home_cn = TEAM_NAMES.get(home, home)
        away_cn = TEAM_NAMES.get(away, away)

        # --- Pinnacle ---
        pin_key = (home, away)
        if pin_key in pinnacle_lookup:
            home_odds, away_odds = pinnacle_lookup[pin_key]
            pin_home_prob, pin_away_prob = remove_vig(home_odds, away_odds)
        else:
            # Try fuzzy match by checking if team names appear anywhere
            pin_home_prob = pin_away_prob = None
            for (h, a), (ho, ao) in pinnacle_lookup.items():
                if (home.lower() in h.lower() or h.lower() in home.lower()) and \
                   (away.lower() in a.lower() or a.lower() in away.lower()):
                    pin_home_prob, pin_away_prob = remove_vig(ho, ao)
                    break

        # --- Polymarket ---
        pm_home_prob, pm_question = find_polymarket_prob(pm_markets, home, away)

        # --- Gap & signals ---
        if pin_home_prob is not None and pm_home_prob is not None:
            gap = pm_home_prob - pin_home_prob
            signal = get_signal(gap)
            signal_class = get_signal_class(gap)
            priority = get_priority(gap)
            priority_key = get_priority_sort_key(gap)
            strategy = get_strategy(pin_home_prob, pin_away_prob)
            strategy_desc = get_strategy_desc(strategy, home_cn, away_cn, pin_home_prob, pm_home_prob)
        else:
            gap = None
            signal = "⚫ 数据缺失"
            signal_class = "signal-gray"
            priority = "—"
            priority_key = 0
            strategy = "N/A"
            strategy_desc = "暂无足够数据进行策略分析。"

        # --- Motivation ---
        home_wins = g["home_wins"] if g["home_wins"] != "N/A" else 0
        home_losses = g["home_losses"] if g["home_losses"] != "N/A" else 0
        away_wins = g["away_wins"] if g["away_wins"] != "N/A" else 0
        away_losses = g["away_losses"] if g["away_losses"] != "N/A" else 0

        try:
            home_mot_level, home_mot_reason = get_motivation(
                g["home_conf_rank"], int(home_wins), int(home_losses)
            )
        except Exception:
            home_mot_level, home_mot_reason = "中", "数据缺失"
        try:
            away_mot_level, away_mot_reason = get_motivation(
                g["away_conf_rank"], int(away_wins), int(away_losses)
            )
        except Exception:
            away_mot_level, away_mot_reason = "中", "数据缺失"

        # --- Focus points ---
        focus_points = []
        if gap is not None and abs(gap) > 0.10:
            if gap > 0:
                focus_points.append(f"Polymarket高估{home_cn}胜率，注意市场情绪是否过热")
            else:
                focus_points.append(f"Polymarket低估{home_cn}胜率，存在价值机会")
        if home_mot_level == "弱":
            focus_points.append(f"{home_cn}动机不足，警惕主队摆烂")
        if away_mot_level == "弱":
            focus_points.append(f"{away_cn}动机不足，警惕客队摆烂")
        if not focus_points:
            focus_points.append("比赛数据正常，持续关注赛前盘口变化")

        rows.append({
            "home": home,
            "away": away,
            "home_cn": home_cn,
            "away_cn": away_cn,
            "game_time_bj": g["game_time_bj"],
            "pin_home_prob": pin_home_prob,
            "pin_away_prob": pin_away_prob,
            "pm_home_prob": pm_home_prob,
            "pm_question": pm_question or "—",
            "gap": gap,
            "signal": signal,
            "signal_class": signal_class,
            "priority": priority,
            "priority_key": priority_key,
            "strategy": strategy,
            "strategy_desc": strategy_desc,
            "home_wins": g["home_wins"],
            "home_losses": g["home_losses"],
            "home_conf": g["home_conf"],
            "home_conf_rank": g["home_conf_rank"],
            "away_wins": g["away_wins"],
            "away_losses": g["away_losses"],
            "away_conf": g["away_conf"],
            "away_conf_rank": g["away_conf_rank"],
            "home_mot_level": home_mot_level,
            "home_mot_reason": home_mot_reason,
            "away_mot_level": away_mot_level,
            "away_mot_reason": away_mot_reason,
            "focus_points": focus_points,
        })

    rows.sort(key=lambda r: r["priority_key"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def motivation_badge(level: str) -> str:
    color_map = {"强": "#22c55e", "中": "#eab308", "弱": "#ef4444"}
    color = color_map.get(level, "#6b7280")
    return f'<span style="color:{color};font-weight:700">{level}</span>'


def render_html(rows: list, update_time: str, today_str: str) -> str:
    if not rows:
        no_data_block = """
        <div class="no-data">
            <div class="no-data-icon">🏀</div>
            <div class="no-data-text">今日暂无比赛数据</div>
            <div class="no-data-sub">请明天再来查看，或手动触发工作流刷新</div>
        </div>
        """
        table_block = no_data_block
        cards_block = ""
    else:
        # Build table rows
        table_rows_html = ""
        for i, r in enumerate(rows):
            matchup = f"{r['away_cn']} @ {r['home_cn']}"
            pin_home = fmt_prob(r["pin_home_prob"]) if r["pin_home_prob"] is not None else "N/A"
            pin_away = fmt_prob(r["pin_away_prob"]) if r["pin_away_prob"] is not None else "N/A"
            pm_price = fmt_prob(r["pm_home_prob"]) if r["pm_home_prob"] is not None else "N/A"
            gap_str = fmt_gap(r["gap"]) if r["gap"] is not None else "N/A"
            signal_html = f'<span class="{r["signal_class"]}">{r["signal"]}</span>'
            priority_html = f'<span class="priority-{r["priority_key"]}">{r["priority"]}</span>'

            table_rows_html += f"""
            <tr onclick="toggleCard('card-{i}')" class="game-row">
                <td class="td-matchup">{matchup}</td>
                <td>{r["game_time_bj"]}</td>
                <td>{pin_home} / {pin_away}</td>
                <td>{pm_price}</td>
                <td class="td-gap">{gap_str}</td>
                <td>{signal_html}</td>
                <td class="td-strategy">{r["strategy"]}</td>
                <td>{priority_html}</td>
            </tr>"""

        # Build detail cards
        cards_html = ""
        for i, r in enumerate(rows):
            home_record = f"{r['home_wins']}胜{r['home_losses']}负" if r["home_wins"] != "N/A" else "战绩未知"
            away_record = f"{r['away_wins']}胜{r['away_losses']}负" if r["away_wins"] != "N/A" else "战绩未知"
            home_rank = f"{r['home_conf']}区第{r['home_conf_rank']}名"
            away_rank = f"{r['away_conf']}区第{r['away_conf_rank']}名"
            focus_html = "".join(f"<li>{fp}</li>" for fp in r["focus_points"])

            pin_home_str = fmt_prob(r["pin_home_prob"]) if r["pin_home_prob"] is not None else "N/A"
            pin_away_str = fmt_prob(r["pin_away_prob"]) if r["pin_away_prob"] is not None else "N/A"
            pm_str = fmt_prob(r["pm_home_prob"]) if r["pm_home_prob"] is not None else "N/A"
            gap_str = fmt_gap(r["gap"]) if r["gap"] is not None else "N/A"

            cards_html += f"""
            <div id="card-{i}" class="detail-card" style="display:none">
                <div class="card-title">📊 {r['away_cn']} @ {r['home_cn']} — 详细分析</div>
                <div class="card-grid">
                    <div class="card-section">
                        <div class="section-title">📋 战绩 &amp; 排名</div>
                        <table class="inner-table">
                            <tr><th></th><th>{r['home_cn']}（主）</th><th>{r['away_cn']}（客）</th></tr>
                            <tr><td>战绩</td><td>{home_record}</td><td>{away_record}</td></tr>
                            <tr><td>分区排名</td><td>{home_rank}</td><td>{away_rank}</td></tr>
                        </table>
                    </div>
                    <div class="card-section">
                        <div class="section-title">💡 动机评估</div>
                        <table class="inner-table">
                            <tr><th>球队</th><th>动机</th><th>原因</th></tr>
                            <tr>
                                <td>{r['home_cn']}</td>
                                <td>{motivation_badge(r['home_mot_level'])}</td>
                                <td>{r['home_mot_reason']}</td>
                            </tr>
                            <tr>
                                <td>{r['away_cn']}</td>
                                <td>{motivation_badge(r['away_mot_level'])}</td>
                                <td>{r['away_mot_reason']}</td>
                            </tr>
                        </table>
                    </div>
                    <div class="card-section">
                        <div class="section-title">📈 胜率对比（主队）</div>
                        <table class="inner-table">
                            <tr><th>来源</th><th>主队胜率</th><th>客队胜率</th></tr>
                            <tr><td>Pinnacle（去水）</td><td>{pin_home_str}</td><td>{pin_away_str}</td></tr>
                            <tr><td>Polymarket</td><td>{pm_str}</td><td>—</td></tr>
                            <tr><td>差值（PM－Pin）</td><td class="{r['signal_class']}">{gap_str}</td><td>—</td></tr>
                        </table>
                    </div>
                    <div class="card-section">
                        <div class="section-title">🎯 策略说明（{r['strategy']}）</div>
                        <p class="strategy-desc">{r['strategy_desc']}</p>
                    </div>
                    <div class="card-section card-section-full">
                        <div class="section-title">🔍 关注点</div>
                        <ul class="focus-list">{focus_html}</ul>
                    </div>
                </div>
            </div>"""

        table_block = f"""
        <div class="table-wrapper">
            <table class="main-table">
                <thead>
                    <tr>
                        <th>比赛（中文）</th>
                        <th>开赛时间（北京）</th>
                        <th>Pinnacle去水胜率（主/客）</th>
                        <th>PM当前价格（主）</th>
                        <th>差值</th>
                        <th>偏差信号</th>
                        <th>策略类型</th>
                        <th>优先级</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows_html}
                </tbody>
            </table>
        </div>
        <p class="click-hint">👆 点击任意行展开/折叠详细分析</p>"""
        cards_block = cards_html

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NBA 每日分析报告</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0f172a;
            color: #e2e8f0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            font-size: 14px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px 16px 60px;
        }}
        /* Header */
        .header {{
            text-align: center;
            padding: 32px 0 24px;
            border-bottom: 1px solid #1e293b;
            margin-bottom: 28px;
        }}
        .header h1 {{
            font-size: clamp(22px, 5vw, 32px);
            font-weight: 800;
            background: linear-gradient(135deg, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 8px;
        }}
        .header .date-line {{
            color: #94a3b8;
            font-size: 13px;
        }}
        .header .update-time {{
            color: #64748b;
            font-size: 12px;
            margin-top: 4px;
        }}
        /* Legend */
        .legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 20px;
            padding: 12px 16px;
            background: #1e293b;
            border-radius: 10px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: #94a3b8;
        }}
        /* Table */
        .table-wrapper {{
            overflow-x: auto;
            border-radius: 12px;
            border: 1px solid #1e293b;
        }}
        .main-table {{
            width: 100%;
            border-collapse: collapse;
            background: #0f172a;
        }}
        .main-table thead tr {{
            background: #1e293b;
        }}
        .main-table th {{
            padding: 12px 14px;
            text-align: left;
            font-weight: 600;
            font-size: 12px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            white-space: nowrap;
        }}
        .main-table td {{
            padding: 12px 14px;
            border-top: 1px solid #1e293b;
            white-space: nowrap;
        }}
        .game-row {{
            cursor: pointer;
            transition: background 0.15s;
        }}
        .game-row:hover {{
            background: #1e293b;
        }}
        .td-matchup {{
            font-weight: 600;
            color: #f1f5f9;
        }}
        .td-gap {{ font-weight: 700; }}
        .td-strategy {{ color: #7dd3fc; }}
        /* Signal colors */
        .signal-red {{ color: #ef4444; font-weight: 600; }}
        .signal-yellow {{ color: #eab308; font-weight: 600; }}
        .signal-green {{ color: #22c55e; font-weight: 600; }}
        .signal-gray {{ color: #6b7280; font-weight: 600; }}
        /* Priority */
        .priority-3 {{ color: #ef4444; font-weight: 700; }}
        .priority-2 {{ color: #eab308; font-weight: 700; }}
        .priority-1 {{ color: #22c55e; font-weight: 600; }}
        .priority-0 {{ color: #6b7280; }}
        /* Click hint */
        .click-hint {{
            text-align: center;
            color: #475569;
            font-size: 12px;
            margin: 10px 0 24px;
        }}
        /* Detail cards */
        .detail-card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }}
        .card-title {{
            font-size: 15px;
            font-weight: 700;
            color: #f1f5f9;
            margin-bottom: 16px;
            padding-bottom: 10px;
            border-bottom: 1px solid #334155;
        }}
        .card-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }}
        .card-section-full {{
            grid-column: 1 / -1;
        }}
        .card-section {{
            background: #0f172a;
            border-radius: 8px;
            padding: 14px;
        }}
        .section-title {{
            font-size: 12px;
            font-weight: 600;
            color: #7dd3fc;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 10px;
        }}
        .inner-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .inner-table th {{
            color: #64748b;
            font-weight: 600;
            padding: 4px 8px 6px 0;
            text-align: left;
            font-size: 11px;
            border-bottom: 1px solid #1e293b;
        }}
        .inner-table td {{
            padding: 5px 8px 5px 0;
            color: #cbd5e1;
            border-bottom: 1px solid #1e293b;
            vertical-align: top;
        }}
        .strategy-desc {{
            font-size: 13px;
            color: #94a3b8;
            line-height: 1.7;
        }}
        .focus-list {{
            list-style: none;
            padding: 0;
        }}
        .focus-list li {{
            font-size: 13px;
            color: #94a3b8;
            padding: 4px 0;
            padding-left: 16px;
            position: relative;
            line-height: 1.6;
        }}
        .focus-list li::before {{
            content: "›";
            position: absolute;
            left: 0;
            color: #38bdf8;
            font-weight: 700;
        }}
        /* No data */
        .no-data {{
            text-align: center;
            padding: 80px 20px;
            background: #1e293b;
            border-radius: 12px;
            border: 1px dashed #334155;
        }}
        .no-data-icon {{ font-size: 48px; margin-bottom: 16px; }}
        .no-data-text {{ font-size: 20px; font-weight: 600; color: #94a3b8; margin-bottom: 8px; }}
        .no-data-sub {{ font-size: 13px; color: #475569; }}
        /* Footer */
        .footer {{
            text-align: center;
            color: #334155;
            font-size: 11px;
            margin-top: 48px;
            line-height: 1.8;
        }}
        /* Responsive */
        @media (max-width: 640px) {{
            .main-table th:nth-child(3),
            .main-table td:nth-child(3) {{ display: none; }}
            .main-table th:nth-child(7),
            .main-table td:nth-child(7) {{ display: none; }}
            .legend {{ gap: 8px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏀 NBA 每日分析报告</h1>
            <div class="date-line">📅 {today_str}（北京时间）</div>
            <div class="update-time">🔄 数据更新时间：{update_time}</div>
        </div>

        <div class="legend">
            <span class="legend-item"><span class="signal-red">●</span> 重大偏差（差值 &gt;10%）— 优先关注</span>
            <span class="legend-item"><span class="signal-yellow">●</span> 中等偏差（差值 5-10%）— 留意</span>
            <span class="legend-item"><span class="signal-green">●</span> 基本一致（差值 &lt;5%）— 正常</span>
            <span class="legend-item"><span class="signal-gray">●</span> 数据缺失 — 暂无参考</span>
        </div>

        {table_block}
        {cards_block}

        <div class="footer">
            <p>数据来源：TheOddsAPI (Pinnacle) · Polymarket · ESPN</p>
            <p>本报告仅供研究参考，不构成任何投资建议。请理性决策，自行承担风险。</p>
            <p>Auto-generated by NBA Daily Analysis · Powered by GitHub Actions</p>
        </div>
    </div>

    <script>
        function toggleCard(id) {{
            var el = document.getElementById(id);
            if (el) {{
                el.style.display = el.style.display === 'none' ? 'block' : 'none';
            }}
        }}
    </script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_bj = datetime.now(BEIJING_TZ)
    today_str = now_bj.strftime("%Y年%m月%d日")
    update_time = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[INFO] NBA Daily Report — {today_str}")
    print("[INFO] Fetching data sources...")

    # Fetch all data sources (graceful failure)
    try:
        pinnacle_data = fetch_pinnacle_odds()
    except Exception:
        pinnacle_data = []
        traceback.print_exc()

    try:
        pm_markets = fetch_polymarket_nba()
    except Exception:
        pm_markets = []
        traceback.print_exc()

    try:
        scoreboard = fetch_espn_scoreboard()
    except Exception:
        scoreboard = {}
        traceback.print_exc()

    try:
        standings_map = fetch_espn_standings()
    except Exception:
        standings_map = {}
        traceback.print_exc()

    # Parse ESPN games
    try:
        espn_games = parse_espn_games(scoreboard, standings_map)
        print(f"[INFO] ESPN: {len(espn_games)} games today.")
    except Exception:
        espn_games = []
        traceback.print_exc()

    # Build analysis rows
    try:
        rows = build_analysis_rows(espn_games, pinnacle_data, pm_markets)
        print(f"[INFO] Analysis complete: {len(rows)} rows.")
    except Exception:
        rows = []
        traceback.print_exc()

    # Generate HTML
    html = render_html(rows, update_time, today_str)

    # Write output
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "index.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"[INFO] Report written to {output_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Sunday-morning lineup recommendations for a Sleeper fantasy football league,
delivered to WhatsApp.

League : 1394837725651177472  (PPR, QB/RB/RB/WR/WR/TE/FLEX/FLEX/DEF/K + 7 BN)
Manager: kingkonguhlig1

Usage:
    python lineup.py                 # normal run, sends WhatsApp if creds present
    python lineup.py --print-only    # console only, no send
    python lineup.py --week 5        # override week
    python lineup.py --final         # "final check" wording (late-morning run)
    python lineup.py --demo          # offline smoke test with fake data
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

LEAGUE_ID = os.getenv("LEAGUE_ID", "1394837725651177472")
USERNAME = os.getenv("SLEEPER_USERNAME", "kingkonguhlig1")

API = "https://api.sleeper.app/v1"
PROJ_API = "https://api.sleeper.app/projections/nfl"
PLAYER_CACHE = os.getenv("PLAYER_CACHE", "players_nfl.json")
CACHE_TTL = 20 * 3600  # players file is huge and changes slowly

# Slot -> eligible positions
FLEX_MAP = {
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}
BENCH_SLOTS = {"BN", "IR", "TAXI"}
SIT_STATUSES = {"Out", "IR", "Doubtful", "Suspended", "PUP", "NA", "DNR"}
WARN_STATUSES = {"Questionable", "Sus", "COV"}

# Sleeper scoring_settings key -> projection stat key (they mostly match 1:1)
STAT_KEYS = (
    "pass_yd pass_td pass_int pass_2pt pass_cmp pass_att pass_fd pass_sack "
    "rush_yd rush_td rush_2pt rush_att rush_fd "
    "rec rec_yd rec_td rec_2pt rec_fd "
    "fum fum_lost fum_rec_td "
    "xpm xpmiss fgm fgmiss fgm_0_19 fgm_20_29 fgm_30_39 fgm_40_49 fgm_50p "
    "fgmiss_0_19 fgmiss_20_29 fgmiss_30_39 fgmiss_40_49 fgmiss_50p "
    "def_td def_st_td def_st_ff def_st_fum_rec def_st_tkl_solo st_td st_ff st_fum_rec st_tkl_solo "
    "sack int fum_rec safe blk_kick pts_allow_0 pts_allow_1_6 pts_allow_7_13 "
    "pts_allow_14_20 pts_allow_21_27 pts_allow_28_34 pts_allow_35p "
    "yds_allow_0_100 yds_allow_100_199 yds_allow_200_299 yds_allow_300_349 "
    "yds_allow_350_399 yds_allow_400_449 yds_allow_450_499 yds_allow_500p"
).split()


# ---------------------------------------------------------------- http helpers
def get_json(url: str, tries: int = 4) -> Any:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sleeper-lineup-bot/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def load_players() -> Dict[str, dict]:
    """~5MB payload. Cache it; Sleeper asks you not to hammer this endpoint."""
    if os.path.exists(PLAYER_CACHE) and time.time() - os.path.getmtime(PLAYER_CACHE) < CACHE_TTL:
        with open(PLAYER_CACHE) as f:
            return json.load(f)
    data = get_json(f"{API}/players/nfl")
    slim = {
        pid: {
            "full_name": p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or pid,
            "position": p.get("position"),
            "fantasy_positions": p.get("fantasy_positions") or [],
            "team": p.get("team"),
            "injury_status": p.get("injury_status"),
            "injury_notes": (p.get("injury_body_part") or ""),
            "status": p.get("status"),
        }
        for pid, p in data.items()
    }
    try:
        with open(PLAYER_CACHE, "w") as f:
            json.dump(slim, f)
    except OSError:
        pass
    return slim


# ------------------------------------------------------------------- scoring
def score_from_stats(stats: dict, scoring: dict) -> float:
    total = 0.0
    for k in STAT_KEYS:
        mult = scoring.get(k)
        val = stats.get(k)
        if mult and val:
            total += float(mult) * float(val)
    # bonus keys (bonus_rec_te, bonus_pass_yd_300, ...) come as thresholds we
    # can't reliably project from means, so we skip them deliberately.
    return round(total, 2)


def projected_points(proj_rows: List[dict], scoring: dict, ppr_mode: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in proj_rows:
        pid = str(row.get("player_id"))
        stats = row.get("stats") or {}
        custom = score_from_stats(stats, scoring)
        fallback = stats.get(ppr_mode) or stats.get("pts_ppr") or stats.get("pts_std") or 0.0
        # Trust our scoring math, but if it collapses to ~0 while Sleeper has a
        # number (missing stat keys, DEF weirdness), use Sleeper's.
        out[pid] = round(custom if custom > 0.5 else float(fallback), 2)
    return out


def ppr_mode_for(scoring: dict) -> str:
    rec = float(scoring.get("rec", 0) or 0)
    return "pts_ppr" if rec >= 0.75 else ("pts_half_ppr" if rec >= 0.25 else "pts_std")


# ------------------------------------------------------------------ optimizer
def eligible(slot: str, pos: str, fantasy_positions: List[str]) -> bool:
    allowed = FLEX_MAP.get(slot, {slot})
    if slot == "DEF":
        allowed = {"DEF", "DST"}
    cands = set(fantasy_positions or []) | ({pos} if pos else set())
    return bool(cands & allowed)


def optimize(slots: List[str], pool: List[dict]) -> List[Tuple[str, Optional[dict]]]:
    """Exact max-projection assignment via DP over a bitmask of used players."""
    n = len(pool)
    if n == 0:
        return [(s, None) for s in slots]
    elig = [[j for j in range(n) if eligible(s, pool[j]["pos"], pool[j]["fpos"])] for s in slots]
    # states: mask -> (total, list of chosen indices aligned to slots)
    states: Dict[int, Tuple[float, List[Optional[int]]]] = {0: (0.0, [])}
    for si in range(len(slots)):
        nxt: Dict[int, Tuple[float, List[Optional[int]]]] = {}
        for mask, (tot, picks) in states.items():
            options = [j for j in elig[si] if not mask >> j & 1] or [None]
            for j in options:
                nmask = mask if j is None else mask | 1 << j
                ntot = tot + (0.0 if j is None else pool[j]["proj"])
                cur = nxt.get(nmask)
                if cur is None or ntot > cur[0]:
                    nxt[nmask] = (ntot, picks + [j])
        # prune: keep best states only (bounded memory on big rosters)
        states = dict(sorted(nxt.items(), key=lambda kv: -kv[1][0])[:40000])
    best = max(states.values(), key=lambda v: v[0])[1]
    return [(slots[i], None if best[i] is None else pool[best[i]]) for i in range(len(slots))]


# -------------------------------------------------------------------- report
def build_report(final: bool, week_override: Optional[int], demo: bool) -> Tuple[str, str]:
    if demo:
        data = demo_payload()
    else:
        state = get_json(f"{API}/state/nfl")
        season = state["season"]
        week = week_override or state.get("display_week") or state["week"] or 1
        league = get_json(f"{API}/league/{LEAGUE_ID}")
        users = get_json(f"{API}/league/{LEAGUE_ID}/users")
        rosters = get_json(f"{API}/league/{LEAGUE_ID}/rosters")
        matchups = get_json(f"{API}/league/{LEAGUE_ID}/matchups/{week}") or []
        players = load_players()
        pos_q = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
        proj_rows = get_json(
            f"{PROJ_API}/{season}/{week}?season_type=regular&{pos_q}&order_by=ppr"
        )
        data = dict(season=season, week=week, league=league, users=users, rosters=rosters,
                    matchups=matchups, players=players, proj_rows=proj_rows)

    season, week = data["season"], data["week"]
    league, users, rosters = data["league"], data["users"], data["rosters"]
    matchups, players, proj_rows = data["matchups"], data["players"], data["proj_rows"]

    scoring = league.get("scoring_settings") or {}
    slots = [s for s in league.get("roster_positions", []) if s not in BENCH_SLOTS]
    pts = projected_points(proj_rows, scoring, ppr_mode_for(scoring))

    me = next((u for u in users if (u.get("display_name") or "").lower() == USERNAME.lower()), None)
    if me is None:
        raise SystemExit(f"Username {USERNAME!r} not found in league {LEAGUE_ID}")
    roster = next((r for r in rosters if r.get("owner_id") == me["user_id"]), None)
    if roster is None:
        raise SystemExit("No roster found for that user in this league")

    my_mu = next((m for m in matchups if m.get("roster_id") == roster["roster_id"]), {})
    current = list(my_mu.get("starters") or roster.get("starters") or [])
    current_set = {p for p in current if p and p != "0"}
    ir = set(roster.get("reserve") or [])

    pool, benched_out = [], []
    for pid in roster.get("players") or []:
        if pid in ir:
            continue
        p = players.get(pid, {})
        rec = {
            "id": pid,
            "name": p.get("full_name", pid),
            "pos": p.get("position"),
            "fpos": p.get("fantasy_positions") or [],
            "team": p.get("team"),
            "status": p.get("injury_status"),
            "proj": pts.get(pid, 0.0),
        }
        if not rec["team"]:
            rec["status"] = rec["status"] or "FA"
        if rec["status"] in SIT_STATUSES or rec["proj"] <= 0:
            benched_out.append(rec)
            if rec["status"] in SIT_STATUSES:
                continue  # never start a confirmed-out player
        pool.append(rec)

    assignment = optimize(slots, pool)
    rec_ids = {p["id"] for _, p in assignment if p}
    rec_total = round(sum(p["proj"] for _, p in assignment if p), 1)
    cur_total = round(sum(pts.get(pid, 0.0) for pid in current_set), 1)

    benchme = [p for p in pool if p["id"] in current_set and p["id"] not in rec_ids]
    startme = [p for _, p in assignment if p and p["id"] not in current_set]

    # opponent context
    opp_line = ""
    if my_mu.get("matchup_id"):
        opp = next((m for m in matchups
                    if m.get("matchup_id") == my_mu["matchup_id"]
                    and m.get("roster_id") != roster["roster_id"]), None)
        if opp:
            oid = next((r["owner_id"] for r in rosters if r["roster_id"] == opp["roster_id"]), None)
            oname = next((u.get("display_name") for u in users if u["user_id"] == oid), "Opponent")
            otot = round(sum(pts.get(p, 0.0) for p in (opp.get("starters") or []) if p and p != "0"), 1)
            diff = round(rec_total - otot, 1)
            opp_line = f"vs {oname}: {rec_total} - {otot} ({'+' if diff >= 0 else ''}{diff})"

    tag = "FINAL CHECK" if final else "LINEUP"
    L: List[str] = [f"*Wk {week} {tag}* - {league.get('name','League')}"]
    if opp_line:
        L.append(opp_line)
    L.append("")
    for slot, p in assignment:
        if p is None:
            L.append(f"{slot:<5} -- EMPTY SLOT --")
            continue
        mark = "*" if p["id"] not in current_set else " "
        flag = ""
        if p["status"] in WARN_STATUSES:
            flag = f" ({p['status'][:1]})"
        elif p["status"]:
            flag = f" ({p['status']})"
        L.append(f"{mark}{slot:<5} {p['name'][:18]:<18} {p['team'] or 'FA':<3} {p['proj']:>5.1f}{flag}")
    L.append("")
    L.append(f"Proj: {rec_total}  (current lineup {cur_total}, +{round(rec_total - cur_total, 1)})")

    if startme or benchme:
        L.append("")
        L.append("*CHANGES*")
        for p in sorted(startme, key=lambda x: -x["proj"]):
            L.append(f"IN  {p['name']} ({p['proj']})")
        for p in sorted(benchme, key=lambda x: -x["proj"]):
            L.append(f"OUT {p['name']} ({p['proj']})")
    else:
        L.append("")
        L.append("No changes - lineup is optimal.")

    watch = [p for _, p in assignment if p and (p["status"] in WARN_STATUSES)]
    if watch:
        L.append("")
        L.append("*WATCH* " + ", ".join(f"{p['name']} {p['status']}" for p in watch))

    sat_out = [p for p in benched_out if p["status"] in SIT_STATUSES and p["id"] in current_set]
    if sat_out:
        L.append("MUST MOVE: " + ", ".join(f"{p['name']} {p['status']}" for p in sat_out))

    if not final:
        L.append("")
        L.append("Inactives drop 11:30am ET - recheck before lock.")

    body = "\n".join(L)
    subject = f"Week {week} {'final check' if final else 'lineup'} - {season}"
    return subject, body


def demo_payload() -> dict:
    """Offline fixture so the script can be smoke-tested without network."""
    slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "DEF", "K"] + ["BN"] * 7
    P = {
        "1": ("Josh Allen", "QB", "BUF", None), "2": ("Bijan Robinson", "RB", "ATL", None),
        "3": ("Kyren Williams", "RB", "LAR", "Questionable"), "4": ("Ja'Marr Chase", "WR", "CIN", None),
        "5": ("Puka Nacua", "WR", "LAR", None), "6": ("Trey McBride", "TE", "ARI", None),
        "7": ("Chase Brown", "RB", "CIN", None), "8": ("Jaxon Smith-Njigba", "WR", "SEA", None),
        "9": ("Ravens", "DEF", "BAL", None), "10": ("Chris Boswell", "K", "PIT", None),
        "11": ("Tony Pollard", "RB", "TEN", None), "12": ("Jerry Jeudy", "WR", "CLE", None),
        "13": ("Rome Odunze", "WR", "CHI", None), "14": ("Tucker Kraft", "TE", "GB", None),
        "15": ("Bo Nix", "QB", "DEN", None), "16": ("Rachaad White", "RB", "TB", "Out"),
        "17": ("Texans", "DEF", "HOU", None),
    }
    proj = {"1": 22.4, "2": 19.8, "3": 14.1, "4": 20.2, "5": 16.9, "6": 13.7, "7": 15.4,
            "8": 15.1, "9": 8.2, "10": 8.9, "11": 9.3, "12": 10.4, "13": 11.8, "14": 9.9,
            "15": 17.2, "16": 0.0, "17": 6.1}
    players = {pid: {"full_name": v[0], "position": v[1], "fantasy_positions": [v[1]],
                     "team": v[2], "injury_status": v[3]} for pid, v in P.items()}
    proj_rows = [{"player_id": pid, "stats": {"pts_ppr": pt}} for pid, pt in proj.items()]
    starters = ["1", "2", "16", "4", "5", "14", "11", "12", "17", "10"]  # deliberately bad
    return dict(
        season="2025", week=6,
        league={"name": "Demo League", "roster_positions": slots, "scoring_settings": {"rec": 1.0}},
        users=[{"user_id": "u1", "display_name": USERNAME},
               {"user_id": "u2", "display_name": "RivalManager"}],
        rosters=[{"roster_id": 1, "owner_id": "u1", "players": list(P), "starters": starters, "reserve": []},
                 {"roster_id": 2, "owner_id": "u2", "players": [], "starters": [], "reserve": []}],
        matchups=[{"roster_id": 1, "matchup_id": 1, "starters": starters},
                  {"roster_id": 2, "matchup_id": 1, "starters": ["1", "4", "6"]}],
        players=players, proj_rows=proj_rows,
    )


# ------------------------------------------------------------------- delivery
def post(url: str, data: bytes, headers: dict) -> str:
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode()[:300]


def send_email(subject: str, text: str) -> bool:
    """Sends the lineup via SMTP. Works with Gmail app passwords, Outlook, etc."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "587")
    user = (os.getenv("SMTP_USER") or "").strip()
    # Google displays App Passwords as "abcd efgh ijkl mnop". Those spaces are
    # display formatting only; SMTP AUTH rejects them. Strip all whitespace.
    password = "".join((os.getenv("SMTP_PASS") or "").split())
    to_addr = (os.getenv("EMAIL_TO") or "").strip() or user
    from_addr = (os.getenv("EMAIL_FROM") or "").strip() or user

    if not (user and password and to_addr):
        print("Email: not configured, skipping.")
        return False

    raw_len = len(os.getenv("SMTP_PASS") or "")
    if raw_len != len(password):
        print(f"  (stripped whitespace from SMTP_PASS: {raw_len} -> {len(password)} chars)")
    if "gmail" in host and len(password) != 16:
        print(f"  WARNING: Gmail App Passwords are exactly 16 characters; after "
              f"stripping, yours is {len(password)}. Regenerate at "
              f"https://myaccount.google.com/apppasswords", file=sys.stderr)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    # Strip the WhatsApp-style asterisks; they are noise in an email client.
    body = "\n".join(ln.replace("*", "") for ln in text.splitlines())
    msg.set_content(body, charset="utf-8")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=45,
                                  context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.ehlo()
                if os.getenv("SMTP_STARTTLS", "1") != "0":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if password != "-":
                    s.login(user, password)
                s.send_message(msg)
        print(f"Email sent to {to_addr} via {host}:{port}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Email send FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        if "5.7.8" in str(e) or "Username and Password not accepted" in str(e):
            print("  Gmail rejected the login. You must use a 16-character App "
                  "Password, not your normal account password, and 2-Step "
                  "Verification must be on.", file=sys.stderr)
        return False


def _mask(v: Optional[str]) -> str:
    if not v:
        return "NOT SET"
    return f"set ({len(v)} chars, ends ...{v[-4:]})"


def report_config() -> None:
    print("--- delivery config ---")
    print(f"  CALLMEBOT_PHONE      {_mask(os.getenv('CALLMEBOT_PHONE'))}")
    print(f"  CALLMEBOT_APIKEY     {_mask(os.getenv('CALLMEBOT_APIKEY'))}")
    print(f"  TWILIO_ACCOUNT_SID   {_mask(os.getenv('TWILIO_ACCOUNT_SID'))}")
    print(f"  TWILIO_AUTH_TOKEN    {_mask(os.getenv('TWILIO_AUTH_TOKEN'))}")
    print(f"  TWILIO_WHATSAPP_FROM {_mask(os.getenv('TWILIO_WHATSAPP_FROM'))}")
    print(f"  WHATSAPP_TO          {_mask(os.getenv('WHATSAPP_TO'))}")
    print(f"  WHATSAPP_TOKEN       {_mask(os.getenv('WHATSAPP_TOKEN'))}")
    print(f"  WHATSAPP_PHONE_ID    {_mask(os.getenv('WHATSAPP_PHONE_ID'))}")
    print(f"  SMTP_USER            {_mask(os.getenv('SMTP_USER'))}")
    print(f"  SMTP_PASS            {_mask(os.getenv('SMTP_PASS'))}")
    print(f"  EMAIL_TO             {os.getenv('EMAIL_TO') or os.getenv('SMTP_USER') or 'NOT SET'}")
    print("-----------------------")


def send_whatsapp(text: str) -> bool:
    """Tries Twilio -> Meta Cloud API -> CallMeBot. Loud about what it did."""
    sent = False
    attempted = False

    sid, tok = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    t_from, t_to = os.getenv("TWILIO_WHATSAPP_FROM"), os.getenv("WHATSAPP_TO")
    if sid and tok and t_from and t_to:
        attempted = True
        import base64
        fields = {"From": t_from, "To": t_to}
        content_sid = os.getenv("TWILIO_CONTENT_SID")
        if content_sid:
            fields["ContentSid"] = content_sid
            fields["ContentVariables"] = json.dumps({"1": text[:1000]})
        else:
            fields["Body"] = text[:1550]
        body = urllib.parse.urlencode(fields).encode()
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        try:
            resp = post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", body,
                        {"Authorization": f"Basic {auth}",
                         "Content-Type": "application/x-www-form-urlencoded"})
            print("Twilio accepted the message.")
            print(f"  Twilio response: {resp[:200]}")
            print("  NOTE: 'queued' is not 'delivered'. Check the Twilio console "
                  "Messaging logs if it never arrives (24h window / template rules).")
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"Twilio send FAILED: {e}", file=sys.stderr)
    else:
        print("Twilio: not configured, skipping.")

    token, phone_id = os.getenv("WHATSAPP_TOKEN"), os.getenv("WHATSAPP_PHONE_ID")
    if not sent and token and phone_id and os.getenv("WHATSAPP_TO"):
        attempted = True
        to = os.getenv("WHATSAPP_TO", "").replace("whatsapp:", "").lstrip("+")
        payload = json.dumps({"messaging_product": "whatsapp", "to": to,
                              "type": "text", "text": {"body": text[:4000]}}).encode()
        try:
            resp = post(f"https://graph.facebook.com/v21.0/{phone_id}/messages", payload,
                        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            print(f"Meta Cloud API accepted. Response: {resp[:200]}")
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"Meta send FAILED: {e}", file=sys.stderr)
    elif not sent:
        print("Meta Cloud API: not configured, skipping.")

    cb_phone, cb_key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not sent and cb_phone and cb_key:
        attempted = True
        if not cb_phone.startswith("+"):
            print(f"WARNING: CALLMEBOT_PHONE is {cb_phone!r} - it must start with "
                  f"'+' and the country code, e.g. +15551234567", file=sys.stderr)
        q = urllib.parse.urlencode({"phone": cb_phone, "text": text[:3800], "apikey": cb_key})
        try:
            resp = get_json_or_text(f"https://api.callmebot.com/whatsapp.php?{q}")
            low = resp.lower()
            bad = ("apikey" in low and "invalid" in low) or "error" in low or "not allowed" in low
            print(f"CallMeBot response: {resp[:250]}")
            if bad:
                print("CallMeBot REJECTED the request - see the response text above. "
                      "Most common causes: wrong apikey, phone number missing '+country code', "
                      "or you never messaged the bot to authorise it.", file=sys.stderr)
            else:
                print("CallMeBot accepted the message.")
                sent = True
        except Exception as e:  # noqa: BLE001
            print(f"CallMeBot send FAILED: {e}", file=sys.stderr)
    elif not sent:
        print("CallMeBot: not configured, skipping.")

    if not attempted:
        print("No WhatsApp provider configured.")
    elif not sent:
        print("\nA WhatsApp provider was configured but the send did not succeed.",
              file=sys.stderr)
    return sent


def deliver(subject: str, text: str) -> bool:
    """Sends via every configured channel. True if at least one succeeded."""
    report_config()
    ok_email = send_email(subject, text)
    ok_wa = send_whatsapp(text)
    if not (ok_email or ok_wa):
        print("\nNothing was delivered. Configure email (SMTP_USER / SMTP_PASS / "
              "EMAIL_TO) or a WhatsApp provider.", file=sys.stderr)
    return ok_email or ok_wa


def get_json_or_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "sleeper-lineup-bot/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode(errors="ignore")[:300]


def auto_decide() -> Tuple[bool, bool]:
    """Returns (should_run, is_final_check) based on US Eastern time.

    This lets the workflow carry duplicate winter/summer crons without ever
    double-texting you, and keeps all date logic in Python instead of shell.
    """
    if os.getenv("GITHUB_EVENT_NAME", "") not in ("schedule", ""):
        return True, False  # manual run: always send
    try:
        import datetime
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return True, False
    if now.weekday() != 6:  # not Sunday
        return False, False
    if now.hour == 9 and now.minute < 40:
        return True, False
    if now.hour == 11 and now.minute >= 30:
        return True, True
    if now.hour == 12 and now.minute < 15:
        return True, True
    return False, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--print-only", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--auto", action="store_true",
                    help="decide run/skip and plan-vs-final from Eastern time")
    ap.add_argument("--check", action="store_true",
                    help="send a one-line test message and report config")
    a = ap.parse_args()

    if a.auto:
        should_run, is_final = auto_decide()
        if not should_run:
            print("Outside the Sunday send window (ET) - skipping, no message sent.")
            return
        a.final = a.final or is_final

    if a.check:
        ok = deliver("Sleeper lineup bot test",
                     "Sleeper lineup bot test message - if you can read this, "
                     "delivery works.")
        raise SystemExit(0 if ok else 1)

    subject, body = build_report(a.final, a.week, a.demo)
    print(subject)
    print(body)
    if not a.print_only and not a.demo:
        if not deliver(subject, body):
            # Fail the workflow so a silent non-delivery shows up as a red X
            # instead of a misleading green check.
            raise SystemExit(1)


if __name__ == "__main__":
    main()

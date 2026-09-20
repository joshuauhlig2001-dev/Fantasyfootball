# Sleeper -> WhatsApp Sunday Lineup Bot

Sunday-morning optimal-lineup recommendations for Sleeper league
`1394837725651177472` (manager **kingkonguhlig1**), pushed to WhatsApp.

- **Format:** Full PPR
- **Starters:** QB, RB, RB, WR, WR, TE, FLEX, FLEX, DEF, K (10) + 7 BN
- **Runs:** Sunday ~9:00am ET (plan) and ~11:45am ET (final check, after inactives)
- **Dependencies:** none - Python 3.9+ standard library only

The bot is **read-only**. It tells you what to change; you tap it in the Sleeper app.

## Quick local test

```bash
python lineup.py --demo        # offline fixture, no network, no send
python lineup.py --print-only  # real league data, console only
python lineup.py --week 5 --print-only
```

## Setup

1. Create a new **private** GitHub repo and push `lineup.py` + `.github/workflows/lineup.yml`.
2. Add secrets under **Settings -> Secrets and variables -> Actions**.
3. Run **Actions -> Sleeper lineup -> WhatsApp -> Run workflow** to test immediately.

## WhatsApp delivery - pick one

### A) CallMeBot (easiest, free, messages yourself)
1. Save `+34 644 51 95 23` as a contact.
2. WhatsApp it: `I allow callmebot to send me messages`
3. You get an API key back.

Secrets: `CALLMEBOT_PHONE` = `+1XXXXXXXXXX`, `CALLMEBOT_APIKEY` = your key.

Third-party relay, unofficial, best-effort uptime. Fine for fantasy football.

### B) Twilio WhatsApp
Secrets: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_WHATSAPP_FROM` = `whatsapp:+14155238886`,
`WHATSAPP_TO` = `whatsapp:+1XXXXXXXXXX`

> **Important 24-hour-window gotcha:** WhatsApp only allows *freeform* business
> messages within 24 hours of your last inbound message. A weekly Sunday alert is
> outside that window, so it will silently fail unless you either
> (a) WhatsApp the bot number each Saturday to reopen the window, or
> (b) create an approved template with one `{{1}}` variable and set
> `TWILIO_CONTENT_SID`. The script handles (b) automatically when that secret exists.
> The Twilio **sandbox** also expires after 72 hours of inactivity - re-send the
> `join <code>` message to reconnect.

### C) Meta WhatsApp Cloud API
Secrets: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_TO` = `1XXXXXXXXXX`.
Same 24-hour template rule applies. Test tokens expire in 24h - use a System User token.

Providers are tried in order Twilio -> Meta -> CallMeBot; the first configured
one that succeeds wins.

## Sample output

```
*Wk 6 LINEUP* - Your League
vs RivalManager: 154.7 - 131.2 (+23.5)

 QB    Josh Allen         BUF  22.4
 RB    Bijan Robinson     ATL  19.8
*RB    Kyren Williams     LAR  14.1 (Q)
 WR    Ja'Marr Chase      CIN  20.2
 WR    Puka Nacua         LAR  16.9
*TE    Trey McBride       ARI  13.7
*FLEX  Chase Brown        CIN  15.4
*FLEX  Jaxon Smith-Njigba SEA  15.1
 DEF   Ravens             BAL   8.2
 K     Chris Boswell      PIT   8.9

Proj: 154.7  (current lineup 123.9, +30.8)

*CHANGES*
IN  Chase Brown (15.4)
OUT Tony Pollard (9.3)

*WATCH* Kyren Williams Questionable
MUST MOVE: Rachaad White Out

Inactives drop 11:30am ET - recheck before lock.
```

`*` marks a player not currently in your lineup.

## How it works

| Step | Detail |
|---|---|
| Week | `GET /v1/state/nfl` (`display_week`) |
| Slots + scoring | `GET /v1/league/{id}` -> `roster_positions`, `scoring_settings` |
| Your roster | `/users` to map `kingkonguhlig1` -> `user_id`, then `/rosters` |
| Current starters | `/matchups/{week}` for the live week's starters |
| Projections | `api.sleeper.app/projections/nfl/{season}/{week}` |
| Points | Computed from raw projected stats x your `scoring_settings`, falling back to `pts_ppr` |
| Optimizer | Exact DP over a bitmask of used players - true maximum, not greedy |

Players marked Out / IR / Doubtful / Suspended are excluded from the pool and
surfaced under `MUST MOVE` if you currently have them starting. Questionable
players are still eligible but flagged under `WATCH`.

## Caveats worth knowing

- The projections endpoint is **undocumented** and may change or rate-limit without notice.
- Projections ignore threshold bonuses (`bonus_rec_te`, `bonus_pass_yd_300`, etc.).
- Raw projections are a floor, not gospel - they don't know about weather, beat-reporter
  snap chatter, or a RB2 who just got promoted. Treat `CHANGES` as a prompt to look, not an order.
- The 9am run happens **before** the 11:30am ET inactive report; the 11:45am run is the one that matters most.
- `players_nfl.json` is ~5MB and cached ~20h. Don't remove the cache step.
- GitHub Actions cron can run late under load, and Actions disables schedules on repos with
  no activity for 60 days - push a commit occasionally in the offseason.

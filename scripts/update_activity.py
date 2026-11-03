"""Generates assets/activity.svg and assets/languages.svg from live GitHub data.
Run by .github/workflows/activity.yml. Needs env GITHUB_TOKEN. Stdlib only."""
import json, os, urllib.request
from datetime import date, datetime, timezone
from html import escape

USER = os.environ.get("GH_USER", "senirualuthge")
TOKEN = os.environ["GITHUB_TOKEN"]

QUERY = """
query($login:String!){ user(login:$login){
  followers{totalCount}
  contributionsCollection{
    totalCommitContributions totalPullRequestContributions totalIssueContributions
    contributionCalendar{ totalContributions weeks{ contributionDays{ date contributionCount } } }
  }
  repositories(ownerAffiliations:OWNER, isFork:false, privacy:PUBLIC, first:100){
    totalCount
    nodes{ languages(first:5, orderBy:{field:SIZE,direction:DESC}){ edges{ size node{ name color } } } }
  }
}}"""

def gql():
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"login": USER}}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if "errors" in data:
        raise SystemExit(data["errors"])
    return data["data"]["user"]

BG, PANEL, LINE, INK, MUTE, VIOLET, CYAN = "#0D1117", "#161B22", "#30363D", "#FFFFFF", "#8B949E", "#8B5CF6", "#22D3EE"
FONT = "font-family=\"-apple-system,Segoe UI,Helvetica,Arial,sans-serif\""

def streaks(days):
    best = cur = 0
    for d in days:
        cur = cur + 1 if d["contributionCount"] else 0
        best = max(best, cur)
    # current streak: count back from today, allowing today to be empty
    run, rev = 0, list(reversed(days))
    if rev and rev[0]["contributionCount"] == 0:
        rev = rev[1:]
    for d in rev:
        if d["contributionCount"]: run += 1
        else: break
    return run, best

def activity_svg(u):
    cc = u["contributionsCollection"]
    cal = cc["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"] if d["date"] <= date.today().isoformat()]
    last = days[-30:]
    cur, best = streaks(days)
    stats = [
        ("Contributions, last year", cal["totalContributions"]),
        ("Commits", cc["totalCommitContributions"]),
        ("Current streak (days)", cur),
        ("Longest streak (days)", best),
    ]
    W, H = 800, 290
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="GitHub activity overview for {escape(USER)}">',
           f'<rect width="{W}" height="{H}" rx="12" fill="{BG}" stroke="{LINE}"/>',
           f'<text x="28" y="40" {FONT} font-size="20" font-weight="700" fill="{INK}">Activity overview</text>',
           f'<text x="{W-28}" y="40" text-anchor="end" {FONT} font-size="12" fill="{MUTE}">Updated {datetime.now(timezone.utc):%Y-%m-%d} UTC</text>']
    cw = (W - 56 - 3 * 12) / 4
    for i, (label, val) in enumerate(stats):
        x = 28 + i * (cw + 12)
        out += [f'<rect x="{x}" y="60" width="{cw}" height="72" rx="8" fill="{PANEL}" stroke="{LINE}"/>',
                f'<text x="{x+14}" y="96" {FONT} font-size="26" font-weight="700" fill="{INK}">{val:,}</text>',
                f'<text x="{x+14}" y="118" {FONT} font-size="12" fill="{MUTE}">{escape(label)}</text>']
    out.append(f'<text x="28" y="164" {FONT} font-size="13" fill="{MUTE}">Contributions per day, last 30 days</text>')
    top, base = 176, 266
    mx = max([d["contributionCount"] for d in last] + [1])
    bw = (W - 56) / max(len(last), 1)
    for i, d in enumerate(last):
        n = d["contributionCount"]
        h = 3 if n == 0 else max(6, (n / mx) * (base - top))
        x = 28 + i * bw
        out.append(f'<rect x="{x+2:.1f}" y="{base-h:.1f}" width="{bw-4:.1f}" height="{h:.1f}" rx="2" fill="{VIOLET if n else LINE}"><title>{d["date"]}: {n}</title></rect>')
    out.append(f'<text x="28" y="282" {FONT} font-size="11" fill="{MUTE}">{last[0]["date"][5:] if last else ""}</text>')
    out.append(f'<text x="{W-28}" y="282" text-anchor="end" {FONT} font-size="11" fill="{MUTE}">Today</text>')
    out.append("</svg>")
    return "\n".join(out)

def languages_svg(u):
    totals, colors = {}, {}
    for repo in u["repositories"]["nodes"]:
        for e in repo["languages"]["edges"]:
            n = e["node"]["name"]
            totals[n] = totals.get(n, 0) + e["size"]
            colors[n] = e["node"]["color"] or VIOLET
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:6]
    s = sum(v for _, v in top) or 1
    W, H = 800, 60 + 28 * len(top) + 20
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="Language breakdown">',
           f'<rect width="{W}" height="{H}" rx="12" fill="{BG}" stroke="{LINE}"/>',
           f'<text x="28" y="38" {FONT} font-size="16" font-weight="700" fill="{INK}">Languages across public repositories</text>']
    for i, (name, v) in enumerate(top):
        y = 62 + i * 28
        pct = v / s * 100
        out += [f'<text x="28" y="{y+12}" {FONT} font-size="13" fill="{INK}">{escape(name)}</text>',
                f'<rect x="150" y="{y+2}" width="540" height="12" rx="6" fill="{LINE}"/>',
                f'<rect x="150" y="{y+2}" width="{max(540*pct/100, 6):.1f}" height="12" rx="6" fill="{colors[name]}"/>',
                f'<text x="{W-28}" y="{y+12}" text-anchor="end" {FONT} font-size="13" fill="{MUTE}">{pct:.1f}%</text>']
    out.append("</svg>")
    return "\n".join(out)

if __name__ == "__main__":
    user = gql()
    os.makedirs("assets", exist_ok=True)
    open("assets/activity.svg", "w").write(activity_svg(user))
    open("assets/languages.svg", "w").write(languages_svg(user))
    print("ok")

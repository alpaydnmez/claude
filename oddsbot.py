#!/usr/bin/env python3
"""
OddsBot - %100 geçmiş orana dayalı "sürpriz oran" tarayıcı
==========================================================
Mantık: Bugünkü maçın oran parmak izine (1, X, 2, 2.5 Üst/Alt) tarihsel olarak
en çok benzeyen maçları bulur, o maçlarda her sonucun GERÇEKTE kaç kez geldiğini
sayar ve bunu bahis şirketinin ima ettiği olasılıkla kıyaslar. Gerçek sıklık,
oranın ima ettiğinden anlamlı biçimde yüksekse (pozitif EV) yüksek oranlı seçimi bildirir.

Veri: football-data.co.uk (ücretsiz CSV, API anahtarı yok). Fikstür: fixtures.csv.
Telegram: TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ORTAM DEĞİŞKENİ olarak verilir.
Kod içine ASLA token yazma.

Komutlar:
  python oddsbot.py ingest [--seasons 10] [--dir klasor]   geçmiş arşivi doldur
  python oddsbot.py backtest --seasons-test 1              yöntem gerçekten işe yarıyor mu?
  python oddsbot.py scan [--days 2]                        günün taraması + Telegram
  python oddsbot.py audit                                  biten maçları karnele
  python oddsbot.py bot                                    Telegram'da /tara /karne /kota
  python oddsbot.py export [--out dashboard.html]          verileri gösteren HTML panel
"""
import argparse, csv, io, json, math, os, sqlite3, sys, time, urllib.request
from datetime import datetime, date, timedelta

DB = os.getenv("OPTA_DB", "oddsbot.db")
PFX = os.getenv("ODDS_PREFIX", "B365")
BASE = "https://www.football-data.co.uk"
DIVS = ["E0", "E1", "E2", "E3", "SC0", "D1", "D2", "I1", "I2", "SP1", "SP2",
        "F1", "F2", "N1", "B1", "P1", "T1", "G1"]
NAMES = {"1": "MS 1", "X": "MS X", "2": "MS 2", "O25": "2.5 Üst", "U25": "2.5 Alt"}
SURPRISE_HTFT = {"X/1", "X/2", "1/2", "2/1"}


class C:  # ayarlar (argümanlarla ezilebilir)
    min_odd = 4.0      # "sürpriz" eşiği
    min_n = 40         # en az benzer maç sayısı
    min_wins = 5       # en az gerçekleşme sayısı
    min_ev = 0.10      # en az beklenen değer (%10)
    k = 40.0           # piyasaya çekme gücü (büyük = daha temkinli)
    tol = 0.06         # başlangıç oran toleransı (±%6)
    max_tol = 0.18
    z = 1.28           # Wilson alt sınırı (~%90 tek taraflı)
    top = 8


# ----------------------------------------------------------------- yardımcılar
def ff(x):
    try:
        v = float(x)
        return v if v > 1.0 else None
    except Exception:
        return None


def fi(x):
    try:
        return int(float(x))
    except Exception:
        return None


def pdate(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date().isoformat()
        except Exception:
            pass
    return None


def wilson_lo(w, n, z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d


def res(a, b):
    return "1" if a > b else ("X" if a == b else "2")


def outcomes(hg, ag, hth=None, hta=None):
    ft = res(hg, ag)
    o = {"1": ft == "1", "X": ft == "X", "2": ft == "2",
         "O25": hg + ag >= 3, "U25": hg + ag <= 2}
    if hth is not None and hta is not None:
        o["HTFT:%s/%s" % (res(hth, hta), ft)] = True
    return o


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "oddsbot/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except Exception as e:
            if i == tries - 1:
                print("İndirilemedi:", url, e, file=sys.stderr)
                return None
            time.sleep(2)


def season_codes(n):
    t = date.today()
    y = t.year if t.month >= 8 else t.year - 1
    return ["%02d%02d" % ((y - i) % 100, (y - i + 1) % 100) for i in range(n)]


# ----------------------------------------------------------------- veritabanı
def db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS matches(
        div TEXT, date TEXT, home TEXT, away TEXT,
        hg INT, ag INT, hth INT, hta INT,
        oh REAL, od REAL, oa REAL, oo REAL, ou REAL,
        UNIQUE(div,date,home,away));
    CREATE INDEX IF NOT EXISTS ix_odds ON matches(oh,od,oa);
    CREATE INDEX IF NOT EXISTS ix_date ON matches(date);
    CREATE TABLE IF NOT EXISTS predictions(
        created TEXT, kickoff TEXT, div TEXT, home TEXT, away TEXT,
        market TEXT, odd REAL, p_post REAL, ev REAL, n INT, w INT,
        status TEXT DEFAULT 'PENDING', score TEXT DEFAULT '', clv REAL,
        UNIQUE(kickoff,home,away,market));
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return con


def meta_get(con, k, d=""):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r[0] if r else d


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    con.commit()


def ingest_text(con, text, div_default=""):
    n = 0
    for r in csv.DictReader(io.StringIO(text)):
        d = pdate(r.get("Date"))
        hg, ag = fi(r.get("FTHG")), fi(r.get("FTAG"))
        home, away = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
        oh, od, oa = (ff(r.get(PFX + k)) for k in "HDA")
        if not d or hg is None or ag is None or not home or None in (oh, od, oa):
            continue
        con.execute("INSERT OR IGNORE INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ((r.get("Div") or div_default), d, home, away, hg, ag,
                     fi(r.get("HTHG")), fi(r.get("HTAG")), oh, od, oa,
                     ff(r.get(PFX + ">2.5")), ff(r.get(PFX + "<2.5"))))
        n += 1
    con.commit()
    return n


def cmd_ingest(a):
    con = db()
    total = 0
    if a.dir:
        for f in sorted(os.listdir(a.dir)):
            if f.lower().endswith(".csv"):
                with open(os.path.join(a.dir, f), encoding="utf-8-sig", errors="replace") as fh:
                    total += ingest_text(con, fh.read(), f[:-4])
    else:
        for s in season_codes(a.seasons):
            for d in DIVS:
                t = get("%s/mmz4281/%s/%s.csv" % (BASE, s, d))
                if t:
                    total += ingest_text(con, t, d)
                time.sleep(0.3)
    cnt = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print("Eklenen/okunan satır: %d | Arşivdeki toplam maç: %d" % (total, cnt))


# ----------------------------------------------------------------- analiz
def neighbors(con, oh, od, oa, oo, before=None):
    t = C.tol
    while True:
        sql = ("SELECT hg,ag,hth,hta,oo FROM matches WHERE "
               "oh BETWEEN ? AND ? AND od BETWEEN ? AND ? AND oa BETWEEN ? AND ?")
        args = [oh / (1 + t), oh * (1 + t), od / (1 + t), od * (1 + t), oa / (1 + t), oa * (1 + t)]
        if before:
            sql += " AND date<?"
            args.append(before)
        rows = con.execute(sql, args).fetchall()
        if oo:
            rows = [r for r in rows if r[4] and abs(math.log(r[4] / oo)) <= t * 1.5]
        if len(rows) >= C.min_n or t >= C.max_tol:
            return rows, t
        t += 0.02


def analyze(con, fx, before=None, offered=None):
    """fx: dict(oh,od,oa,oo,ou). Döner: (picks, htft_bilgi, n, tol)"""
    oh, od, oa, oo, ou = fx["oh"], fx["od"], fx["oa"], fx.get("oo"), fx.get("ou")
    rows, tol = neighbors(con, oh, od, oa, oo, before)
    n = len(rows)
    if n < C.min_n:
        return [], [], n, tol
    inv = [1 / oh, 1 / od, 1 / oa]
    s = sum(inv)
    prior = {"1": inv[0] / s, "X": inv[1] / s, "2": inv[2] / s}
    odds = {"1": oh, "X": od, "2": oa}
    if oo and ou:
        po = (1 / oo) / (1 / oo + 1 / ou)
        prior["O25"], prior["U25"] = po, 1 - po
        odds["O25"], odds["U25"] = oo, ou
    cnt, n_ht = {}, 0
    for hg, ag, hth, hta, _ in rows:
        for m, hit in outcomes(hg, ag, hth, hta).items():
            if hit:
                cnt[m] = cnt.get(m, 0) + 1
        if hth is not None:
            n_ht += 1
    picks = []
    for m, odd in odds.items():
        w = cnt.get(m, 0)
        p_post = (w + C.k * prior[m]) / (n + C.k)
        ev = p_post * odd - 1
        if odd >= C.min_odd and w >= C.min_wins and ev >= C.min_ev:
            picks.append(dict(market=m, odd=odd, p_post=p_post, ev=ev, n=n, w=w,
                              prior=prior[m], p_lo=wilson_lo(w, n, C.z)))
    htft = []
    if n_ht >= C.min_n:
        for m, w in cnt.items():
            if not m.startswith("HTFT:"):
                continue
            c = m[5:]
            p_lo = wilson_lo(w, n_ht, C.z)
            if c in SURPRISE_HTFT and w >= 3:
                be = 1 / p_lo if p_lo > 0 else 99
                off = (offered or {}).get(c)
                htft.append(dict(combo=c, w=w, n=n_ht, p=w / n_ht, p_lo=p_lo,
                                 fair=n_ht / w, breakeven=be, offered=off))
                if off and p_lo * off - 1 >= 0 and w >= C.min_wins and off >= C.min_odd:
                    picks.append(dict(market="HTFT:" + c, odd=off, p_post=p_lo,
                                      ev=p_lo * off - 1, n=n_ht, w=w, prior=0, p_lo=p_lo))
    picks.sort(key=lambda x: -x["ev"])
    htft.sort(key=lambda x: -x["p_lo"])
    return picks, htft, n, tol


def mname(m):
    return NAMES.get(m, "İY/MS " + m[5:] if m.startswith("HTFT:") else m)


# ----------------------------------------------------------------- Telegram
import html as _html
import re as _re
try:
    from zoneinfo import ZoneInfo
    TR_TZ, UK_TZ = ZoneInfo("Europe/Istanbul"), ZoneInfo("Europe/London")
except Exception:
    TR_TZ = UK_TZ = None

MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
          "Eylül", "Ekim", "Kasım", "Aralık"]
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
ENG = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
SCO = "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"
LEAGUES = {"E0": (ENG, "Premier League"), "E1": (ENG, "Championship"), "E2": (ENG, "League One"),
           "E3": (ENG, "League Two"), "SC0": (SCO, "İskoç Premiership"), "D1": ("🇩🇪", "Bundesliga"),
           "D2": ("🇩🇪", "2. Bundesliga"), "I1": ("🇮🇹", "Serie A"), "I2": ("🇮🇹", "Serie B"),
           "SP1": ("🇪🇸", "La Liga"), "SP2": ("🇪🇸", "La Liga 2"), "F1": ("🇫🇷", "Ligue 1"),
           "F2": ("🇫🇷", "Ligue 2"), "N1": ("🇳🇱", "Eredivisie"), "B1": ("🇧🇪", "Pro League"),
           "P1": ("🇵🇹", "Primeira Liga"), "T1": ("🇹🇷", "Süper Lig"), "G1": ("🇬🇷", "Süper Lig (GR)")}
MARKET_ICON = {"1": "🏠", "X": "🤝", "2": "✈️", "O25": "🔥", "U25": "🧊"}


def E(s):
    return _html.escape(str(s), quote=False)


def tr_now():
    if TR_TZ:
        return datetime.now(TR_TZ).replace(tzinfo=None)
    return datetime.utcnow() + timedelta(hours=3)


def tr_date_str(d):
    return "%d %s %s" % (d.day, MONTHS[d.month - 1], DAYS[d.weekday()])


def kickoff_tr(d, t):
    """football-data tarih+saat (UK saati) -> Türkiye saati (naive datetime)"""
    base = datetime.strptime(d, "%Y-%m-%d")
    try:
        hh, mm = (t or "12:00").strip().split(":")[:2]
        dt = base.replace(hour=int(hh), minute=int(mm))
    except Exception:
        dt = base.replace(hour=12)
    if UK_TZ and TR_TZ:
        return dt.replace(tzinfo=UK_TZ).astimezone(TR_TZ).replace(tzinfo=None)
    return dt + timedelta(hours=3)


def stars(ev, w):
    s = 1
    if ev >= 0.20 and w >= 6:
        s = 2
    if ev >= 0.35 and w >= 10:
        s = 3
    return "⭐" * s


def tg_api(method, payload, timeout=60):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        return None
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (tok, method),
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print("Telegram hatası:", type(e).__name__, file=sys.stderr)
        return None


def plain(s):
    return _html.unescape(_re.sub(r"<[^>]+>", "", s))


def tg(blocks):
    """blocks: str veya blok listesi. Bloklar arasında boş satır, 3800 karakterde yeni mesaj."""
    if isinstance(blocks, str):
        blocks = [blocks]
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not chat:
        for b in blocks:
            print(plain(b) + "\n")
        return
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > 3800:
            msgs.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    for m in msgs:
        r = tg_api("sendMessage", {"chat_id": chat, "text": m, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
        if not r or not r.get("ok"):   # HTML hatasında düz metinle tekrar dene
            tg_api("sendMessage", {"chat_id": chat, "text": plain(m)})
        time.sleep(0.4)


# ----------------------------------------------------------------- tarama
def load_offered(path):
    """htft_odds.csv: date,home,away,market,odd  (market örn: 1/2)"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            d, o = pdate(r.get("date")), ff(r.get("odd"))
            if d and o:
                out.setdefault((d, r["home"].strip(), r["away"].strip()), {})[r["market"].strip()] = o
    return out


def do_scan(a_days=2, offered_path="htft_odds.csv"):
    con = db()
    if con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] < 1000:
        return ["⚠️ Arşiv boş/yetersiz. Önce arşivi kur: <code>python oddsbot.py ingest</code>"]
    text = get(BASE + "/fixtures.csv")
    if not text:
        return ["⚠️ fixtures.csv indirilemedi. Biraz sonra tekrar dene."]
    now = tr_now()
    today = now.date()
    last_day = today + timedelta(days=a_days)
    key = "scan:" + today.isoformat()
    meta_set(con, key, int(meta_get(con, key, "0") or 0) + 1)
    meta_set(con, "last_scan", time.time())
    offered_all = load_offered(offered_path)

    days, file_max, started = {}, None, 0
    for r in csv.DictReader(io.StringIO(text)):
        d = pdate(r.get("Date"))
        if not d:
            continue
        if not file_max or d > file_max:
            file_max = d
        home, away = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
        oh, od, oa = (ff(r.get(PFX + k)) for k in "HDA")
        if None in (oh, od, oa) or not home:
            continue
        ko = kickoff_tr(d, r.get("Time"))
        if ko.date() < today or ko.date() > last_day:
            continue
        if ko < now:
            started += 1
            continue
        fx = dict(oh=oh, od=od, oa=oa, oo=ff(r.get(PFX + ">2.5")), ou=ff(r.get(PFX + "<2.5")))
        picks, htft, n, tol = analyze(con, fx, offered=offered_all.get((d, home, away)))
        day = days.setdefault(ko.date(), dict(n=0, picks=[], ht=[]))
        day["n"] += 1
        dv = r.get("Div", "")
        for p in picks:
            day["picks"].append((ko, dv, home, away, p, d))
        for h in htft[:1]:
            day["ht"].append((h["p_lo"], ko, home, away, h))

    total = sum(x["n"] for x in days.values())
    blocks = ["🎯 <b>ODDSBOT • SÜRPRİZ TARAMA</b>\n🗓 %s • 🕐 %s (TR)\n🔎 <b>%d</b> maç incelendi • eşik: oran ≥ <b>%.1f</b> • EV ≥ <b>%%%d</b>" %
              (tr_date_str(now), now.strftime("%H:%M"), total, C.min_odd, round(C.min_ev * 100))]
    if today not in days:
        blocks.append("⚠️ <b>Kaynak uyarısı</b>\nKaynak dosyada bugüne ait, başlamamış maç bulunamadı "
                      "(dosyadaki son tarih: <code>%s</code>). football-data bu dosyayı haftada birkaç kez günceller; "
                      "o yüzden bazı günler eksik kalabilir." % E(file_max or "-"))

    shown_ht = []
    for dday in sorted(days):
        day = days[dday]
        day["picks"].sort(key=lambda x: -x[4]["ev"])
        top = day["picks"][:5]
        head = "━━━━━━━━━━━━━━━━━━\n📆 <b>%s</b>\n📋 %d maç tarandı • 🎯 %d fırsat" % (tr_date_str(dday), day["n"], len(day["picks"]))
        blocks.append(head)
        if not top:
            blocks.append("😴 Bu gün için eşiği geçen sürpriz fırsat yok.")
        for ko, dv, home, away, p, fdate in top:
            flag, lname = LEAGUES.get(dv, ("🌍", dv))
            icon = MARKET_ICON.get(p["market"], "⏱️")
            if p["market"].startswith("HTFT"):
                extra = "📊 Geçmişte <code>%d/%d</code> (<b>%%%.1f</b>) • iddaa oranı verdiğin dosyadan" % (p["w"], p["n"], p["w"] / p["n"] * 100)
            else:
                extra = "📊 Geçmişte <code>%d/%d</code> (<b>%%%.1f</b>) • piyasa %%%.1f" % (p["w"], p["n"], p["w"] / p["n"] * 100, p["prior"] * 100)
            blocks.append("⚽ <b>%s – %s</b>\n%s %s • 🕐 %s\n%s <b>%s</b> @ <code>%.2f</code>  %s\n%s\n🧠 Temkinli olasılık %%%.1f • 💰 EV <b>%%%+.0f</b>" %
                          (E(home), E(away), flag, E(lname), ko.strftime("%H:%M"),
                           icon, E(mname(p["market"])), p["odd"], stars(p["ev"], p["w"]),
                           extra, p["p_post"] * 100, p["ev"] * 100))
            con.execute("INSERT OR IGNORE INTO predictions(created,kickoff,div,home,away,market,odd,p_post,ev,n,w) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), fdate, dv, home, away,
                         p["market"], p["odd"], p["p_post"], p["ev"], p["n"], p["w"]))
        shown_ht += day["ht"]
    con.commit()

    if shown_ht:
        shown_ht.sort(key=lambda x: -x[0])
        lines = ["💣 <b>İY/MS fiyat rehberi</b>\n<i>İddaa oranı eşiğin ÜSTÜNDEYSE değerli sayılır.</i>"]
        for p_lo, ko, home, away, h in shown_ht[:6]:
            lines.append("• <b>%s</b> %s–%s (%s)\n   <code>%d/%d</code> (%%%.1f) • adil <code>%.1f</code> • değer eşiği ≥ <code>%.1f</code>" %
                         (h["combo"], E(home), E(away), ko.strftime("%d.%m %H:%M"), h["w"], h["n"],
                          h["p"] * 100, h["fair"], h["breakeven"]))
        blocks.append("\n".join(lines))
    blocks.append("⚠️ <i>Sanal kasada izle. Geçmiş sıklık gelecek garantisi değildir.</i>")
    return blocks


def cmd_scan(a):
    tg(do_scan(a.days))


# ----------------------------------------------------------------- karne
def do_audit():
    con = db()
    pend = con.execute("SELECT rowid,div,kickoff,home,away,market,odd FROM predictions WHERE status='PENDING'").fetchall()
    if not pend:
        return ["📒 <b>ODDSBOT • KARNE</b>\n😴 Bekleyen tahmin yok."]
    texts = {}
    for div in {p[1] for p in pend}:
        t = get("%s/mmz4281/%s/%s.csv" % (BASE, season_codes(1)[0], div))
        if t:
            texts[div] = list(csv.DictReader(io.StringIO(t)))
    settled = 0
    for rid, div, ko, home, away, mk, odd in pend:
        for r in texts.get(div, []):
            if pdate(r.get("Date")) == ko and (r.get("HomeTeam") or "").strip() == home \
                    and (r.get("AwayTeam") or "").strip() == away:
                hg, ag = fi(r.get("FTHG")), fi(r.get("FTAG"))
                if hg is None or ag is None:
                    break
                o = outcomes(hg, ag, fi(r.get("HTHG")), fi(r.get("HTAG")))
                won = bool(o.get(mk))
                clv = None
                if mk in ("1", "X", "2"):
                    c = ff(r.get(PFX + "C" + {"1": "H", "X": "D", "2": "A"}[mk]))
                    clv = odd / c - 1 if c else None
                con.execute("UPDATE predictions SET status=?,score=?,clv=? WHERE rowid=?",
                            ("WON" if won else "LOST", "%d-%d (İY %s-%s)" % (hg, ag, r.get("HTHG"), r.get("HTAG")),
                             clv, rid))
                settled += 1
                break
    con.commit()
    rows = con.execute("SELECT status,odd,clv FROM predictions WHERE status!='PENDING'").fetchall()
    n = len(rows)
    wins = sum(1 for r in rows if r[0] == "WON")
    profit = sum((r[1] - 1) if r[0] == "WON" else -1 for r in rows)
    clvs = [r[2] for r in rows if r[2] is not None]
    last = con.execute("SELECT kickoff,home,away,market,odd,status,score FROM predictions "
                       "WHERE status!='PENDING' ORDER BY rowid DESC LIMIT 8").fetchall()
    now = tr_now()
    blocks = ["📒 <b>ODDSBOT • KARNE</b>\n🗓 %s • 🕐 %s (TR)\n✅ Bu turda sonuçlanan: <b>%d</b> • ⏳ bekleyen: <b>%d</b>" %
              (tr_date_str(now), now.strftime("%H:%M"), settled, len(pend) - settled)]
    if n:
        s = "📈 <b>Genel durum</b>\n🎯 Tahmin: <code>%d</code> • ✅ Tutan: <code>%d</code> (<b>%%%.1f</b>)\n💵 Kâr/zarar: <code>%+.2f br</code> • ROI <b>%%%+.1f</b>" % \
            (n, wins, wins / n * 100, profit, profit / n * 100)
        if clvs:
            s += "\n📐 Ort. CLV: <b>%%%+.1f</b> <i>(kalıcı pozitifse gerçek avantaj işareti)</i>" % (sum(clvs) / len(clvs) * 100)
        blocks.append(s)
    if last:
        lines = ["🧾 <b>Son sonuçlar</b>"]
        for k, h, a_, m, o, st, sc in last:
            lines.append("%s <code>%s</code> %s – %s\n    %s @ <code>%.2f</code> • %s" %
                         ("✅" if st == "WON" else "❌", k[5:].replace("-", "."), E(h), E(a_), E(mname(m)), o, E(sc)))
        blocks.append("\n".join(lines))
    if n < 100:
        blocks.append("ℹ️ <i>%d örnek çok az; yüzlerce tahmin birikmeden sonuca güvenme.</i>" % n)
    return blocks


def cmd_audit(a):
    tg(do_audit())


# ----------------------------------------------------------------- backtest
def cmd_backtest(a):
    con = db()
    cut = (date.today() - timedelta(days=365 * a.seasons_test)).isoformat()
    test = con.execute("SELECT date,hg,ag,hth,hta,oh,od,oa,oo,ou FROM matches WHERE date>=? ORDER BY date", (cut,)).fetchall()
    print("Test maçı: %d (yalnızca her maçtan ÖNCEKİ veri kullanılır)" % len(test))
    P = dict(n=0, w=0, profit=0.0)
    B = dict(n=0, w=0, profit=0.0)
    for d, hg, ag, hth, hta, oh, od, oa, oo, ou in test:
        fx = dict(oh=oh, od=od, oa=oa, oo=oo, ou=ou)
        o = outcomes(hg, ag, hth, hta)
        odds = {"1": oh, "X": od, "2": oa}
        if oo and ou:
            odds["O25"], odds["U25"] = oo, ou
        for m, odd in odds.items():       # taban çizgisi: eşiğin üstündeki HER seçimi oyna
            if odd >= C.min_odd:
                B["n"] += 1
                B["w"] += o[m]
                B["profit"] += (odd - 1) if o[m] else -1
        picks, _, _, _ = analyze(con, fx, before=d)
        for p in picks:
            if p["market"].startswith("HTFT"):
                continue
            P["n"] += 1
            P["w"] += o[p["market"]]
            P["profit"] += (p["odd"] - 1) if o[p["market"]] else -1
    for name, s in (("Taban (eşik üstü hepsini oyna)", B), ("OddsBot seçimleri", P)):
        if s["n"]:
            print("%-32s bahis:%5d  tutan:%4d (%%%.1f)  ROI: %%%+.1f" %
                  (name, s["n"], s["w"], s["w"] / s["n"] * 100, s["profit"] / s["n"] * 100))
        else:
            print("%-32s bahis yok" % name)
    print("\nYorum: OddsBot ROI'si tabandan belirgin iyi DEĞİLSE ve örnek sayısı azsa yöntem kanıtlanmamıştır.")


# ----------------------------------------------------------------- Telegram botu
def cmd_bot(a):
    chat = str(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not chat:
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenlerini ayarla.")
    con = db()
    offset = None
    print("Bot dinliyor...")
    while True:
        r = tg_api("getUpdates", {"timeout": 50, "offset": offset}, timeout=70) or {}
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) != chat:
                continue
            cmd = (m.get("text") or "").split()[0].lower() if m.get("text") else ""
            if cmd == "/tara":
                last = float(meta_get(con, "last_scan", "0") or 0)
                if time.time() - last < 600:
                    tg("⏳ Son taramadan 10 dk geçmedi, biraz bekle (kota koruması).")
                else:
                    tg(do_scan(2))
            elif cmd == "/karne":
                tg(do_audit())
            elif cmd == "/kota":
                k = "scan:" + tr_now().date().isoformat()
                tg("📊 <b>Kota</b>\nBugün <b>%s</b> tarama yapıldı.\nKaynak ücretsiz CSV; yine de 10 dk bekleme uygulanıyor." % meta_get(con, k, "0"))
            else:
                tg("🤖 <b>Komutlar</b>\n/tara – günün taraması\n/karne – sonuç karnesi\n/kota – tarama sayacı")


# ----------------------------------------------------------------- panel (dashboard.html)
DASH_HTML = r"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OddsBot Panel</title>
<style>
:root{--bg:#ffffff;--card:#f3f5f9;--tx:#111827;--mut:#6b7280;--bd:#e2e5ec;--ok:#047857;--bad:#b91c1c;--acc:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#131a2e;--tx:#e5e7eb;--mut:#94a3b8;--bd:#25304d;--ok:#34d399;--bad:#f87171;--acc:#60a5fa}}
*{box-sizing:border-box}
body{margin:0;padding:14px;background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin-inline:auto}
h1{font-size:19px;margin:0 0 2px}h2{font-size:15px;margin:22px 0 8px}
.mut{color:var(--mut);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-top:12px}
.c{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px}
.c span{display:block;font-size:11px;color:var(--mut)}.c b{font-size:18px}
.warn{background:rgba(245,158,11,.15);border:1px solid #f59e0b;border-radius:10px;padding:9px 11px;margin-top:12px;font-size:13px}
.wrap{overflow-x:auto;border:1px solid var(--bd);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:640px}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--bd);white-space:nowrap}
th{background:var(--card);color:var(--mut);font-size:11px;text-transform:uppercase}
tr:last-child td{border-bottom:none}
.ok{color:var(--ok);font-weight:700}.bad{color:var(--bad);font-weight:700}
svg{width:100%;height:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px}
</style></head><body>
<h1>OddsBot Panel</h1>
<div class="mut" id="sub"></div>
<div id="warn"></div>
<div class="cards" id="cards"></div>
<h2>Kümülatif kâr/zarar (1 birim bahis)</h2><div id="chart"></div>
<h2>Oran bandına göre kalibrasyon</h2><div class="wrap"><table id="bands"></table></div>
<h2>Bekleyen tahminler</h2><div class="wrap"><table id="pend"></table></div>
<h2>Sonuçlanan tahminler</h2><div class="wrap"><table id="done"></table></div>
<script>
const D=/*DATA*/null;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pc=(x,d=1)=>(x*100).toFixed(d)+"%";
const P=D.predictions,S=P.filter(p=>p.status!=="PENDING"),pend=P.filter(p=>p.status==="PENDING");
const W=S.filter(p=>p.status==="WON");
const ret=p=>p.status==="WON"?p.odd-1:-1;
const profit=S.reduce((a,p)=>a+ret(p),0);
const clv=S.filter(p=>p.clv!=null).map(p=>p.clv);
const avg=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:0;
$("#sub").textContent="Arşiv: "+D.archive.count+" maç ("+D.archive.min+" – "+D.archive.max+") | Son tarama: "+D.last_scan+" | Üretildi: "+D.generated;
if(S.length<100)$("#warn").innerHTML='<div class="warn">Yalnızca '+S.length+' sonuçlanmış tahmin var. Yüzlerce örnek birikmeden isabet oranı ve ROI güvenilir değildir.</div>';
const card=(k,v)=>'<div class="c"><span>'+k+'</span><b>'+v+'</b></div>';
$("#cards").innerHTML=card("Bekleyen",pend.length)+card("Sonuçlanan",S.length)+
 card("Tutan",W.length)+card("Tutma oranı",S.length?pc(W.length/S.length):"-")+
 card("Kâr/zarar",(profit>=0?"+":"")+profit.toFixed(2)+" br")+
 card("ROI",S.length?(profit/S.length*100).toFixed(1)+"%":"-")+
 card("Ort. CLV",clv.length?(avg(clv)*100).toFixed(1)+"%":"-");
// grafik
const asc=[...S].sort((a,b)=>a.kickoff<b.kickoff?-1:1);
let cum=0;const pts=asc.map(p=>cum+=ret(p));
if(pts.length>1){
 const w=600,h=180,pad=24,mn=Math.min(0,...pts),mx=Math.max(0,...pts),rg=(mx-mn)||1;
 const x=i=>pad+i*(w-2*pad)/(pts.length-1),y=v=>h-pad-(v-mn)*(h-2*pad)/rg;
 const line=pts.map((v,i)=>x(i).toFixed(1)+","+y(v).toFixed(1)).join(" ");
 $("#chart").innerHTML='<svg viewBox="0 0 '+w+' '+h+'"><line x1="'+pad+'" x2="'+(w-pad)+'" y1="'+y(0)+'" y2="'+y(0)+'" stroke="currentColor" stroke-opacity=".3" stroke-dasharray="4"/><polyline fill="none" stroke="var(--acc)" stroke-width="2" points="'+line+'"/><text x="'+pad+'" y="14" font-size="11" fill="currentColor">Son: '+pts[pts.length-1].toFixed(2)+' br</text></svg>';
}else $("#chart").innerHTML='<div class="mut">Grafik için en az 2 sonuçlanmış tahmin gerekir.</div>';
// bantlar
const bands=[["4–6",4,6],["6–10",6,10],["10–20",10,20],["20+",20,1e9]];
let bh="<tr><th>Oran bandı</th><th>Tahmin</th><th>Tuttu</th><th>Gerçekleşen</th><th>Model olasılığı (ort.)</th><th>ROI</th></tr>";
for(const [nm,lo,hi] of bands){
 const s=S.filter(p=>p.odd>=lo&&p.odd<hi);
 if(!s.length){bh+="<tr><td>"+nm+"</td><td colspan=5 class=mut>veri yok</td></tr>";continue}
 const w=s.filter(p=>p.status==="WON").length,pr=s.reduce((a,p)=>a+ret(p),0);
 bh+="<tr><td>"+nm+"</td><td>"+s.length+"</td><td>"+w+"</td><td>"+pc(w/s.length)+"</td><td>"+pc(avg(s.map(p=>p.p_post)))+"</td><td class="+(pr>=0?"ok":"bad")+">"+(pr/s.length*100).toFixed(1)+"%</td></tr>";
}
$("#bands").innerHTML=bh;
// bekleyen
let ph="<tr><th>Tarih</th><th>Lig</th><th>Maç</th><th>Seçim</th><th>Oran</th><th>Geçmiş</th><th>Model p</th><th>EV</th></tr>";
ph+=pend.map(p=>"<tr><td>"+esc(p.kickoff)+"</td><td>"+esc(p.div)+"</td><td>"+esc(p.home)+" - "+esc(p.away)+"</td><td>"+esc(p.market_name)+"</td><td>"+p.odd.toFixed(2)+"</td><td>"+p.w+"/"+p.n+" ("+pc(p.w/p.n)+")</td><td>"+pc(p.p_post)+"</td><td>"+(p.ev*100).toFixed(0)+"%</td></tr>").join("")||"<tr><td colspan=8 class=mut>Bekleyen tahmin yok</td></tr>";
$("#pend").innerHTML=ph;
// sonuçlanan
let dh="<tr><th>Tarih</th><th>Maç</th><th>Seçim</th><th>Oran</th><th>Skor</th><th>Sonuç</th><th>CLV</th></tr>";
dh+=S.map(p=>"<tr><td>"+esc(p.kickoff)+"</td><td>"+esc(p.home)+" - "+esc(p.away)+"</td><td>"+esc(p.market_name)+"</td><td>"+p.odd.toFixed(2)+"</td><td>"+esc(p.score)+"</td><td class="+(p.status==="WON"?"ok":"bad")+">"+(p.status==="WON"?"TUTTU":"YATTI")+"</td><td>"+(p.clv==null?"-":(p.clv*100).toFixed(1)+"%")+"</td></tr>").join("")||"<tr><td colspan=7 class=mut>Henüz sonuçlanan tahmin yok</td></tr>";
$("#done").innerHTML=dh;
</script></body></html>"""


def build_dashboard(con):
    cols = ("created", "kickoff", "div", "home", "away", "market", "odd", "p_post",
            "ev", "n", "w", "status", "score", "clv")
    preds = [dict(zip(cols, r)) for r in con.execute(
        "SELECT %s FROM predictions ORDER BY kickoff DESC, rowid DESC" % ",".join(cols))]
    for p in preds:
        p["market_name"] = mname(p["market"])
    cnt, mn, mx = con.execute("SELECT COUNT(*),MIN(date),MAX(date) FROM matches").fetchone()
    ls = meta_get(con, "last_scan", "")
    try:
        ls = datetime.fromtimestamp(float(ls)).strftime("%d.%m.%Y %H:%M") if ls else "-"
    except Exception:
        ls = "-"
    data = dict(predictions=preds, archive=dict(count=cnt, min=mn or "-", max=mx or "-"),
                last_scan=ls, generated=datetime.now().strftime("%d.%m.%Y %H:%M"))
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return DASH_HTML.replace("/*DATA*/null", payload)


def cmd_export(a):
    con = db()
    html = build_dashboard(con)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("Panel yazıldı:", a.out)


def main():
    ap = argparse.ArgumentParser(description="OddsBot")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("ingest"); p.add_argument("--seasons", type=int, default=10); p.add_argument("--dir")
    p = sp.add_parser("backtest"); p.add_argument("--seasons-test", type=int, default=1)
    p = sp.add_parser("scan"); p.add_argument("--days", type=int, default=2)
    sp.add_parser("audit"); sp.add_parser("bot")
    p = sp.add_parser("export"); p.add_argument("--out", default="dashboard.html")
    for s in sp.choices.values():
        s.add_argument("--min-odd", type=float); s.add_argument("--min-ev", type=float)
        s.add_argument("--min-n", type=int); s.add_argument("--k", type=float)
    a = ap.parse_args()
    for src, dst in (("min_odd", "min_odd"), ("min_ev", "min_ev"), ("min_n", "min_n"), ("k", "k")):
        if getattr(a, src, None) is not None:
            setattr(C, dst, getattr(a, src))
    {"ingest": cmd_ingest, "backtest": cmd_backtest, "scan": cmd_scan,
     "audit": cmd_audit, "bot": cmd_bot, "export": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    main()

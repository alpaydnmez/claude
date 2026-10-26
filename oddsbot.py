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


def tg(text):
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not chat:
        print(text)
        return
    for i in range(0, len(text), 3900):
        tg_api("sendMessage", {"chat_id": chat, "text": text[i:i + 3900]})


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


def do_scan(a_days=2, offered_path="htft_odds.csv", quiet=False):
    con = db()
    if con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] < 1000:
        return "Arşiv boş/yetersiz. Önce: python oddsbot.py ingest"
    text = get(BASE + "/fixtures.csv")
    if not text:
        return "fixtures.csv indirilemedi."
    key = "scan:" + date.today().isoformat()
    meta_set(con, key, int(meta_get(con, key, "0") or 0) + 1)
    meta_set(con, "last_scan", time.time())
    offered_all = load_offered(offered_path)
    today, limit = date.today(), date.today() + timedelta(days=a_days)
    found, htlines, scanned = [], [], 0
    for r in csv.DictReader(io.StringIO(text)):
        d = pdate(r.get("Date"))
        if not d or not (today.isoformat() <= d <= limit.isoformat()):
            continue
        oh, od, oa = (ff(r.get(PFX + k)) for k in "HDA")
        home, away = (r.get("HomeTeam") or "").strip(), (r.get("AwayTeam") or "").strip()
        if None in (oh, od, oa) or not home:
            continue
        fx = dict(oh=oh, od=od, oa=oa, oo=ff(r.get(PFX + ">2.5")), ou=ff(r.get(PFX + "<2.5")))
        picks, htft, n, tol = analyze(con, fx, offered=offered_all.get((d, home, away)))
        scanned += 1
        dv = r.get("Div", "")
        for p in picks:
            found.append((d, r.get("Time", ""), dv, home, away, p))
        for h in htft[:1]:
            htlines.append((h["p_lo"], "%s %s-%s  İY/MS %s: %d/%d (%%%.1f) | adil oran %.1f | ≥%.1f görürsen değer" %
                            (d[5:], home, away, h["combo"], h["w"], h["n"], h["p"] * 100,
                             h["fair"], h["breakeven"])))
    found.sort(key=lambda x: -x[5]["ev"])
    found = found[:C.top]
    lines = ["🎯 OddsBot taraması | %d maç incelendi | eşik ≥%.1f oran, EV ≥ %%%d" %
             (scanned, C.min_odd, C.min_ev * 100)]
    if not found:
        lines.append("\nBu tarama için eşiği geçen sürpriz fırsat YOK. (Normal; çoğu gün böyledir.)")
    for d, t, dv, home, away, p in found:
        lines.append("\n%s %s [%s]\n%s - %s\n➜ %s @ %.2f\nGeçmişte %d/%d (%%%.1f) | piyasa %%%.1f | temkinli olasılık %%%.1f | EV %%%+.0f" %
                     (d, t, dv, home, away, mname(p["market"]), p["odd"], p["w"], p["n"],
                      p["w"] / p["n"] * 100, p["prior"] * 100, p["p_post"] * 100, p["ev"] * 100))
        con.execute("INSERT OR IGNORE INTO predictions(created,kickoff,div,home,away,market,odd,p_post,ev,n,w) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), d, dv, home, away,
                     p["market"], p["odd"], p["p_post"], p["ev"], p["n"], p["w"]))
    con.commit()
    if htlines:
        htlines.sort(reverse=True)
        lines.append("\n💣 İY/MS fiyat rehberi (iddaa oranı bu eşiğin ÜSTÜNDEYSE değer):")
        lines += ["• " + x[1] for x in htlines[:6]]
    lines.append("\n⚠️ Sanal kasada izle; geçmiş sıklık gelecek garantisi değildir.")
    return "\n".join(lines)


def cmd_scan(a):
    tg(do_scan(a.days))


# ----------------------------------------------------------------- karne
def do_audit():
    con = db()
    pend = con.execute("SELECT rowid,div,kickoff,home,away,market,odd FROM predictions WHERE status='PENDING'").fetchall()
    if not pend:
        return "Bekleyen tahmin yok."
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
                if mk in "1X2" and len(mk) == 1:
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
    lines = ["📒 Karne | bu turda sonuçlanan: %d | bekleyen: %d" % (settled, len(pend) - settled),
             "Toplam: %d tahmin | %d tuttu (%%%.1f) | 1 birim bahisle kâr/zarar: %+.2f birim | ROI %%%+.1f" %
             (n, wins, wins / n * 100 if n else 0, profit, profit / n * 100 if n else 0)]
    if clvs:
        lines.append("Ort. CLV (kapanışa karşı): %%%+.1f  (kalıcı pozitifse gerçek avantaj işareti)" % (sum(clvs) / len(clvs) * 100))
    for k, h, a, m, o, st, sc in last:
        lines.append("%s %s %s-%s | %s @%.2f | %s %s" % ("✅" if st == "WON" else "❌", k[5:], h, a, mname(m), o, st, sc))
    if n < 100:
        lines.append("\nℹ️ %d örnek çok az; yüzlerce tahmin birikmeden sonuca güvenme." % n)
    return "\n".join(lines)


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
                    tg("⏳ Son taramadan 10 dk geçmedi, bekle (kota koruması).")
                else:
                    tg(do_scan(2))
            elif cmd == "/karne":
                tg(do_audit())
            elif cmd == "/kota":
                k = "scan:" + date.today().isoformat()
                tg("Bugün %s tarama yapıldı. Kaynak ücretsiz CSV; yine de 10 dk bekleme uygulanıyor." % meta_get(con, k, "0"))
            else:
                tg("Komutlar: /tara  /karne  /kota")


def main():
    ap = argparse.ArgumentParser(description="OddsBot")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("ingest"); p.add_argument("--seasons", type=int, default=10); p.add_argument("--dir")
    p = sp.add_parser("backtest"); p.add_argument("--seasons-test", type=int, default=1)
    p = sp.add_parser("scan"); p.add_argument("--days", type=int, default=2)
    sp.add_parser("audit"); sp.add_parser("bot")
    for s in sp.choices.values():
        s.add_argument("--min-odd", type=float); s.add_argument("--min-ev", type=float)
        s.add_argument("--min-n", type=int); s.add_argument("--k", type=float)
    a = ap.parse_args()
    for src, dst in (("min_odd", "min_odd"), ("min_ev", "min_ev"), ("min_n", "min_n"), ("k", "k")):
        if getattr(a, src, None) is not None:
            setattr(C, dst, getattr(a, src))
    {"ingest": cmd_ingest, "backtest": cmd_backtest, "scan": cmd_scan,
     "audit": cmd_audit, "bot": cmd_bot}[a.cmd](a)


if __name__ == "__main__":
    main()

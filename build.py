import os, sys, json, time, subprocess, urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import OrderedDict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# ================= 基线常量（与 4etf_base_deepseek_v1.py 一致） =================
CODES = ["159915.SZ", "159941.SZ", "518880.SH", "159985.SZ"]
NAMES = {"159915.SZ": "创业板", "159941.SZ": "纳指", "518880.SH": "黄金", "159985.SZ": "豆粕"}
COLORS = {"159915.SZ": "#ef4b4b", "159941.SZ": "#4dd0e1", "518880.SH": "#f5c542", "159985.SZ": "#b07cf0"}
WINDOWS = [21, 22, 23, 24]
ACCEL_W = 0.75
FEE = 0.00006          # 万 0.6，单边
SELL_SLIP = 0.0005     # 卖出滑点 5bp
SIGNAL_START = 42      # 动量预热窗 = max(WINDOWS) + 21
MAX_W = max(WINDOWS)

LISTING = {"159915.SZ": "20111209", "159941.SZ": "20150713",
           "518880.SH": "20130729", "159985.SZ": "20191205"}
START = "20190101"

# ---- 数据源：腾讯 fqkline 前复权日线（不再读 QMT 本地 DAT）----
FQKLINE = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
DATASRC = '腾讯 fqkline 前复权日线（web.ifzq.gtimg.cn）'
TENCENT_TICK = 'http://qt.gtimg.cn/q='   # 实时价兜底（--update 时取一次）
_UA = {'User-Agent': 'Mozilla/5.0'}
PORT = 9999            # 主人 2026-11-03 定：腾讯数据源版用 9999；--port 可覆盖

# 对账基准（基线脚本 20191205~20260921，QMT DAT 数据口径）——腾讯数据源数字会略有不同，仅作参考，不判 PASS
BASE_CHECK = dict(ann=48.30, sharpe=1.47, mdd=26.89, trades=190, nav_last=1313.5968)

# 数据快照（人工更新；服务端只读它，从不取数）
SNAPSHOT = os.path.join(HERE, '4etf_tencent_snapshot.json')


def _age_txt(sec):
    if sec < 90:
        return '刚刚'
    if sec < 3600:
        return '%d 分钟前' % (sec // 60)
    if sec < 86400:
        return '%d 小时前' % (sec // 3600)
    return '%d 天前' % (sec // 86400)


def save_snapshot(data, path=None):
    """原子落盘快照（先写 .tmp 再 replace，避免读到半截文件）。"""
    path = path or SNAPSHOT
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)
    return path


def load_snapshot(path=None):
    """读快照 → (data, meta)。meta 记文件时间/新旧程度/数据时点，供页面展示。"""
    path = path or SNAPSHOT
    if not os.path.exists(path):
        raise FileNotFoundError(
            '数据快照不存在：%s\n请先人工更新数据（双击 update_4etf_tencent_data.bat，或运行：%s --update）'
            % (path, os.path.basename(__file__)))
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    mt = os.path.getmtime(path)
    age = max(0.0, time.time() - mt)
    meta = dict(path=path, mtime=mt,
                mtime_txt=datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M:%S'),
                age_sec=age, age_txt=_age_txt(age), stale=(age > 86400),
                data_asof=data.get('asof', ''), gen_time=data.get('gen_time', ''),
                last_date=(data.get('dates') or ['-'])[-1])
    return data, meta


# ================= 取数：腾讯 fqkline 前复权日线 =================
TENCENT_CODES = {'159915.SZ': 'sz159915', '159941.SZ': 'sz159941',
                 '518880.SH': 'sh518880', '159985.SZ': 'sz159985'}


def _fetch_window(full, end, count=640):
    """腾讯 fqkline 单窗拉取。返回 (bars, src)。
    bar 格式：[date, open, close, high, low, volume]。
    src = 'qfqday'（前复权）或 'day'（不复权；对无分红 ETF 等价前复权）。网络/解析失败返回 ([], None)。"""
    url = (FQKLINE + '?param=%s,day,2000-01-01,%s,%d,qfq' % (full, end, count))
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        d = json.loads(raw)
    except Exception as e:
        print('[WARN] 腾讯 fqkline 取数失败 %r' % (e,), flush=True)
        return [], None
    node = None
    for v in d.get('data', {}).values():
        if isinstance(v, dict) and ('qfqday' in v or 'day' in v):
            node = v
            break
    if node is None:
        return [], None
    if 'qfqday' in node:
        return node.get('qfqday') or [], 'qfqday'
    return node.get('day') or [], 'day'


def _prev_day(s):
    return (datetime.fromisoformat(s) - timedelta(days=1)).date().isoformat()


def fetch_tencent_history(cde):
    """拉取单只 ETF 的完整腾讯前复权日线（翻页拉全，按 end 回退）。

    返回 (OrderedDict(YYYYMMDD->close), OrderedDict(YYYYMMDD->open))，均已 round3。
    前复权：纳指 159941 的 2022 份额拆分已由腾讯复权，无需本端再乘因子。
    159985 无分红，腾讯只给 'day'（不复权）＝前复权，同口径。
    """
    full = TENCENT_CODES[cde]
    end = datetime.now().strftime('%Y-%m-%d')
    by_date = {}
    src = None
    while True:
        bars, s = _fetch_window(full, end)
        if s:
            src = s
        if not bars:
            break
        for b in bars:
            by_date[b[0]] = b
        earliest = bars[0][0]
        if earliest <= '2000-01-01' or len(bars) < 640:
            break
        end = _prev_day(earliest)

    s = OrderedDict()
    so = OrderedDict()
    for d in sorted(by_date):
        b = by_date[d]
        if d < LISTING[cde] or d < START:
            continue
        try:
            o = float(b[1]); c = float(b[2])
        except (ValueError, IndexError):
            continue
        if c <= 0:
            continue
        s[d] = round(c, 3)
        so[d] = round(o, 3) if o > 0 else round(c, 3)
    if not s:
        raise RuntimeError('%s 腾讯数据为空（src=%s）——请检查外网连通性：%s'
                           % (cde, src, FQKLINE))
    return s, so, src


def fetch_closes():
    """拉 4ETF 腾讯前复权日线 close 与 open，round3。

    返回 (frames, frames_o, src_map)：
        frames[cde]   = OrderedDict(YYYYMMDD -> close)
        frames_o[cde] = OrderedDict(YYYYMMDD -> open)
        src_map[cde]  = 'qfqday' | 'day'
    """
    frames = {}
    frames_o = {}
    src_map = {}
    for cde in CODES:
        s, so, src = fetch_tencent_history(cde)
        frames[cde] = s
        frames_o[cde] = so
        src_map[cde] = src
        print('         %s %s  %d 根  %s ~ %s  (%s)'
              % (NAMES[cde], cde, len(s), list(s)[0], list(s)[-1], src), flush=True)
    print('[数据源] %s  %s' % (DATASRC, FQKLINE), flush=True)
    return frames, frames_o, src_map


def fetch_tencent_tick(codes):
    """腾讯实时行情（一次请求批量取 4 只）。返回与行情接口同构的 dict：
        {code: {'lastPrice': 最新价, 'timetag': 'YYYYMMDDHHMMSS', 'volume': 成交量(手)}}

    字段位置（按 ~ 分割）：[2]代码 [3]最新价 [5]今开 [6]成交量 [30]时间 [33]最高 [34]最低
    网络不通 / 接口异常 → 返回 {}，调用方自动退化为日线 bar（不影响出数）。
    """
    syms = ','.join(('sh' if c.endswith('.SH') else 'sz') + c.split('.')[0] for c in codes)
    try:
        raw = subprocess.check_output(['curl', '-s', '-m', '10', TENCENT_TICK + syms],
                                      stderr=subprocess.DEVNULL)
    except Exception as e:
        print('[WARN] 腾讯实时行情取数失败 %r -> 逐只退化为日线收盘价' % (e,), flush=True)
        return {}
    txt = raw.decode('gbk', errors='replace')
    by_code = {c.split('.')[0]: c for c in codes}
    out = {}
    for seg in txt.split(';'):
        seg = seg.strip()
        if '="' not in seg:
            continue
        _, val = seg.split('="', 1)
        f = val.rstrip('"').split('~')
        if len(f) < 35:
            continue
        cde = by_code.get(f[2])
        if not cde:
            continue
        try:
            out[cde] = dict(lastPrice=float(f[3] or 0), timetag=str(f[30] or ''),
                            volume=float(f[6] or 0), open=float(f[5] or 0))
        except ValueError:
            continue
    if out:
        print('[行情] 腾讯实时 tick 命中 %d/%d 只' % (len(out), len(codes)), flush=True)
    return out


def overlay_live_price(frames, frames_o):
    """**每只各自**用实时行情「最新价」覆盖/补齐当日 bar，确保四张图永远落在最新价格上。

    主人原则（2026-09-24）：盘中用最新实时价、盘后用最新收盘价，且**逐只独立**判定，
    一只没成交/没数据不能把其它三只拖回昨天。判定依据是行情返回的价格时点（不是本机时钟猜）：
      ① 该只 tick 有效（lastPrice>0 + 时间戳＝今天 + 今日已有成交 volume>0）
           → 盘中 = 最新实时成交价；盘后 = 当日收盘价
      ② tick 无效但日线已有今天这根 → 该只日线收盘价（在途价）
      ③ 无今日价且仅缺今天（停牌/无成交）→ 最后收盘价持平填充（价格不动，不造假）
      ④ 数据落后多日 → 不填充，页面退到四只共同最后一天（build_data 里补警告）
      非交易日 → 直接用日线最后一根收盘价，不扩展。

    返回 dict(mode/live/px/ts/tick_tag/
        bar_last : {code: 覆盖前日线最后一根日期}（原始值，供页面展示与落后检测）
        per_code : {code: 'tick'|'bar'|'ffill'|'stale'}  每只的取价来源
        note/intraday 同上
    )
    """
    now = datetime.now()
    # 腾讯日线日期键用 %Y-%m-%d（如 2026-11-02）；实时 tick 时间戳用 %Y%m%d%H%M%S。
    # 两者格式不同，分开两个变量：today_key 匹配 frames 键、today_cmp 匹配 tick 时间戳前缀。
    today_key = now.strftime("%Y-%m-%d")
    today_cmp = now.strftime("%Y%m%d")
    bar_last = {c: (list(frames[c].keys())[-1] if frames[c] else None) for c in CODES}
    intraday = (now.hour, now.minute) < (15, 0)
    info = dict(mode='close_bar', live=False, px={}, ts='', tick_tag=None,
                bar_last=bar_last, per_code={}, note='', intraday=intraday)

    if now.weekday() >= 5:
        info['note'] = '非交易日 → 日线 bar 收盘价'
        info['ts'] = max([v for v in bar_last.values() if v] or [''])
        info['px'] = {c: round(float(frames[c][bar_last[c]]), 4) for c in CODES}
        info['per_code'] = {c: 'stale' for c in CODES}
        return info

    tk = fetch_tencent_tick(CODES) or None

    # ---- 逐只判定：每只独立取它能拿到的最新价，不再 all-or-nothing ----
    plan = {}     # code -> (price, src)
    tags = {}
    u = sorted(set.union(*[set(f.keys()) for f in frames.values()]))
    prev_day = u[-2] if len(u) >= 2 else None
    for c in CODES:
        d = (tk or {}).get(c) or {}
        p = float(d.get('lastPrice') or 0)
        t = str(d.get('timetag') or d.get('time') or '')
        v = float(d.get('volume') or 0)
        tags[c] = t
        if p > 0 and t.startswith(today_cmp) and v > 0:
            plan[c] = (round(p, 3), 'tick')
        elif today_key in frames[c]:
            plan[c] = (round(float(frames[c][today_key]), 3), 'bar')
        elif u and u[-1] == today_key and bar_last[c] == prev_day:
            # 仅缺今天（其它日期齐全）→ 停牌/无成交，按最后收盘价持平
            plan[c] = (round(float(frames[c][bar_last[c]]), 3), 'ffill')
        # else：数据落后多日 → 不填充，交给共同日期轴回退（build_data 补警告）

    for c, (p, src) in plan.items():
        frames[c][today_key] = p
        frames_o[c][today_key] = float(frames_o[c].get(today_key, p))   # 今开缺失/持平 → open=close（今日内涨跌 0）

    # ---- 汇总（逐只）----
    used = {c: s for c, (_, s) in plan.items()}
    info['per_code'] = {c: (used.get(c) or 'stale') for c in CODES}
    n_tick = sum(1 for s in used.values() if s == 'tick')
    n_bar = sum(1 for s in used.values() if s == 'bar')
    n_ff = sum(1 for s in used.values() if s == 'ffill')
    n_stale = len(CODES) - len(used)

    if n_tick > 0:
        ft = next(c for c in CODES if used.get(c) == 'tick')
        info.update(mode=('intraday' if intraday else 'close_tick'), live=True,
                    ts=tags[ft], tick_tag=tags[ft])
    elif n_bar > 0:
        info.update(mode='close_bar', live=False, ts=today_key)
    else:
        info.update(mode='close_bar', live=False,
                    ts=max([v for v in bar_last.values() if v] or ['']))

    parts = []
    if n_tick:
        parts.append(('实时 tick 价' if intraday else '当日收盘(tick)') + ' ×%d' % n_tick)
    if n_bar:
        parts.append('日线收盘 ×%d' % n_bar)
    if n_ff:
        parts.append('今日无成交、按最后收盘持平（%s）×%d'
                     % ('、'.join(NAMES[c] for c in CODES if used.get(c) == 'ffill'), n_ff))
    if n_stale:
        parts.append('日线未刷新（%s）×%d'
                     % ('、'.join(NAMES[c] for c in CODES if c not in used), n_stale))
    info['note'] = (('盘中 → ' if intraday and n_tick else '') + '·'.join(parts)) if parts else '日线 bar 收盘价'
    if n_stale:
        print("[WARN] 日线未刷新 %s -> 页面退到共同最后一天" %
              [(c, bar_last[c]) for c in CODES if c not in used], flush=True)
    info['px'] = {c: (plan[c][0] if c in plan else round(float(frames[c][bar_last[c]]), 4)) for c in CODES}
    return info


# ================= 打分 / 选股（与基线逐字一致） =================
def compute_scores(mat):
    n = mat.shape[0]
    scores = np.zeros((n, len(CODES)))
    for t in range(SIGNAL_START, n):
        for w in WINDOWS:
            mom = mat[t] / mat[t - w] - 1
            for pos, idx in enumerate(np.argsort(mom)):
                scores[t, idx] += pos + 1
        r21_t = mat[t] / mat[t - 21] - 1
        r21_prev = mat[t - 21] / mat[t - 42] - 1
        accel = r21_t - r21_prev
        for pos, idx in enumerate(np.argsort(accel)):
            scores[t, idx] += ACCEL_W * (pos + 1)
    return scores


def choose_top1(t, scores, mat, current_hold):
    """三级决胜：Composite → RawMomentum → 保持当前持仓。返回 (idx, l2, l3)"""
    row = scores[t]
    candidates = np.flatnonzero(row == np.max(row))
    if len(candidates) == 1:
        return int(candidates[0]), 0, 0
    raw = np.zeros(len(CODES))
    for w in WINDOWS:
        raw += mat[t] / mat[t - w] - 1
    raw /= len(WINDOWS)
    best_raw = np.max(raw[candidates])
    rc = candidates[raw[candidates] == best_raw]
    if len(rc) == 1:
        return int(rc[0]), 1, 0
    if current_hold in rc:
        return int(current_hold), 1, 1
    return int(rc[0]), 1, 1


def met(seq):
    """基线 met() 同口径：从序列第 0 个元素起算。"""
    s = np.asarray(seq, float)
    m = len(s)
    if m < 2:
        return dict(ann=0.0, sharpe=0.0, sortino=0.0, mdd=0.0, calmar=0.0, total=0.0)
    ann = (s[-1] / s[0]) ** (252 / max(m - 1, 1)) - 1
    r = np.diff(s) / s[:-1]
    sh = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0
    dv = np.sqrt(np.mean(np.minimum(r, 0) ** 2)) * np.sqrt(252)
    dd = 1 - s / np.maximum.accumulate(s)
    mdd = float(dd.max())
    return dict(ann=ann * 100, sharpe=sh, sortino=ann / dv if dv > 0 else 0,
                mdd=mdd * 100, calmar=ann / mdd if mdd > 0 else 0,
                total=(s[-1] / s[0] - 1) * 100)


def run_baseline(mat):
    """与基线 run_base() 逐字一致。"""
    n = mat.shape[0]
    scores = compute_scores(mat)
    top1 = np.empty(n, dtype=int)
    top1[:SIGNAL_START] = 0
    l2 = l3 = 0
    for t in range(SIGNAL_START, n):
        cur = int(top1[t - 1]) if t > SIGNAL_START else 0
        top1[t], a, b = choose_top1(t, scores, mat, cur)
        l2 += a; l3 += b
    hold = np.empty(n, dtype=int)
    hold[:SIGNAL_START] = top1[SIGNAL_START]
    hold[SIGNAL_START:] = top1[SIGNAL_START - 1:n - 1]
    nav = np.empty(n)
    nav[:SIGNAL_START] = 100.0
    trades = 0
    for t in range(SIGNAL_START, n):
        prev_, cur_ = int(hold[t - 1]), int(hold[t])
        nav[t] = nav[t - 1] * (mat[t, cur_] / mat[t - 1, cur_])
        if cur_ != prev_:
            nav[t] *= (1 - SELL_SLIP) * (1 - FEE) * (1 - FEE)
            trades += 1
    return nav, hold, top1, trades, l2, l3, scores


def run_v2co(mat, mat_o, dates=None):
    """收卖开买（v2co）：t 日收盘信号 → t 日收盘价卖旧 → **t+1 日开盘价买新**。

    逐字复刻 4etf_v2co.py::run_base（2026-09-24 修复后版本 = 主人定稿口径）：
      · 买入日：nav *= (1-FEE) × (close[t]/open[t])   —— 只吃 open→close 段，隔夜裸露
      · 持有日：nav *= close[t]/close[t-1]            —— 吃满全天（含隔夜）
      · 卖出日：先计当日全天涨跌，再 ×(1-FEE)         —— 零滑点
      · **T+1**：买入日不检查当日信号（当天买入的仓位当天卖不掉，最快 t+2）→ 换仓日 t+1 不重复决策
      · 末日不卖出（无 t+1 可再买，卖出 = 无故清仓）
    返回 (nav, hold, pend, target_last, segs, trades, fee_total)
      hold[t]  = 当日实际持仓（-1 = 空仓：已卖待买 / 预热期）
      pend[t]  = 当日收盘已卖出、待次日开盘买入的目标（-1 = 无）
    """
    n = mat.shape[0]
    scores = compute_scores(mat)
    top1 = np.empty(n, dtype=int)
    top1[:SIGNAL_START] = 0
    cur_hold = -1
    for t in range(SIGNAL_START, n):
        top1[t], _, _ = choose_top1(t, scores, mat, cur_hold)
        cur_hold = int(top1[t])

    nav = np.empty(n)
    nav[:SIGNAL_START] = 100.0
    hold = np.full(n, -1, dtype=int)
    pend = np.full(n, -1, dtype=int)
    cur = -1
    in_pos = False
    entry_px = 0.0
    entry_i = -1
    trades = 0
    fee_total = 0.0
    segs = []

    def _d(i):
        return (dates[i] if dates and i < len(dates) else str(i))

    for t in range(SIGNAL_START, n):
        tgt = int(top1[t])
        if cur < 0:
            # 首个信号日：t 日收盘信号 → 待 t+1 开盘买入，t 日无持仓、无收益
            if t + 1 < n:
                cur = tgt
                in_pos = False
            nav[t] = nav[t - 1]
        else:
            if not in_pos:
                # 昨日收盘卖出后空仓过夜，今日开盘买入（成交价 = 今日开盘集合竞价撮合价）
                entry_i = t
                entry_px = mat_o[t, cur]
                in_pos = True
                nav[t] = nav[t - 1] * (mat[t, cur] / mat_o[t, cur]) * (1 - FEE)
                fee_total += FEE
            else:
                # 持有中：昨日收盘 → 今日收盘（吃满全天，含隔夜）
                nav[t] = nav[t - 1] * (mat[t, cur] / mat[t - 1, cur])
                if tgt != cur and t < n - 1:
                    # t 日收盘卖出旧仓（零滑点），状态转为空仓，等 t+1 开盘买入
                    nav[t] *= (1 - FEE)
                    fee_total += FEE
                    rg = float(mat[t, cur] / entry_px - 1)
                    rn = float((1 + rg) * (1 - FEE) * (1 - FEE) - 1)
                    segs.append(dict(etf=NAMES[CODES[cur]], code=CODES[cur],
                                     entry=_d(entry_i), exit=_d(t), days=t - entry_i + 1,
                                     entry_i=int(entry_i), exit_i=int(t), ki=int(cur),
                                     ep=round(float(entry_px), 4), xp=round(float(mat[t, cur]), 4),
                                     ret=round(rn * 100, 2), ret_gross=round(rg * 100, 2),
                                     open_flag=False))
                    cur = tgt
                    in_pos = False
                    trades += 1
        hold[t] = cur if in_pos else -1
        if hold[t] < 0 and cur >= 0:
            pend[t] = cur                    # 空仓 → 记录待次日开盘买入的目标

    if in_pos and entry_i >= SIGNAL_START:   # 末日未平仓段（同口径预扣卖出佣金）
        rg = float(mat[n - 1, cur] / entry_px - 1)
        rn = float((1 + rg) * (1 - FEE) * (1 - FEE) - 1)
        segs.append(dict(etf=NAMES[CODES[cur]], code=CODES[cur],
                         entry=_d(entry_i), exit=_d(n - 1), days=n - 1 - entry_i + 1,
                         entry_i=int(entry_i), exit_i=int(n - 1), ki=int(cur),
                         ep=round(float(entry_px), 4), xp=round(float(mat[n - 1, cur]), 4),
                         ret=round(rn * 100, 2), ret_gross=round(rg * 100, 2),
                         open_flag=True))
    return nav, hold, pend, int(top1[n - 1]), segs, trades, fee_total


def run_ew(mat):
    n = mat.shape[0]
    ew = np.empty(n)
    ew[:MAX_W] = 100.0
    for t in range(MAX_W, n):
        ew[t] = ew[t - 1] * np.mean(mat[t] / mat[t - 1])
    return ew


# ================= 组装工作台数据 =================
def build_data(end=None):
    """读腾讯日线 → 补实时价 → 全量重算。数据源见 fetch_closes()。"""
    t0 = time.time()
    frames, frames_o, src_map = fetch_closes()
    px_info = overlay_live_price(frames, frames_o)
    fetch_sec = time.time() - t0

    common = sorted(set.intersection(*[set(f.keys()) for f in frames.values()]))
    if end:
        common = [d for d in common if d <= end]
    else:
        # 数据落后检测：某只原始日线末根早于最晚标的 → 整页被它拽回共同最后一天，写明是谁拖的
        max_raw = max([px_info['bar_last'].get(c) or '' for c in CODES] or [''])
        laggards = [c for c in CODES if (px_info['bar_last'].get(c) or '') < max_raw]
        if laggards and max_raw > common[-1]:
            nm = '、'.join('%s 停在 %s' % (NAMES[c], px_info['bar_last'][c]) for c in laggards)
            px_info['note'] += ('；%s 日线未刷新（其余到 %s）→ 页面退到四只共同最后一天 %s'
                                % (nm, max_raw, common[-1]))
    n = len(common)
    if n <= SIGNAL_START + 5:
        raise RuntimeError(f"可用交易日不足（{n} 天 < {SIGNAL_START + 5}）")
    mat   = np.array([[frames[c][d]   for c in CODES] for d in common], dtype=float)
    mat_o = np.array([[frames_o[c][d] for c in CODES] for d in common], dtype=float)

    nav_base, hold_base, top1, trades_base, l2, l3, scores = run_baseline(mat)
    ew = run_ew(mat)

    # ---- v2co（收卖开买）：与基线共用同一份数据、同一套信号，只差买入时点 ----
    nav_co, hold_co, pend_co, tgt_co, segs_co, trades_co, fee_co = run_v2co(mat, mat_o, common)

    # 主人 2026-09-24 定：工作台主体呈现 **v2co** 的数据；基线留作对照曲线。
    nav, hold, trades = nav_co, hold_co, trades_co

    # ---- 持仓段明细：直接用 v2co 引擎产出（主人 2026-09-24：主体数据切 v2co） ----
    segs = segs_co
    wins = sum(1 for s in segs if s['ret'] > 0)
    seg_prod = 1.0
    gap_prod = 1.0                       # 段间漏掉的隔夜累计：Π(入场日开盘 / 前一日收盘)，信息指标，不参与闭合
    for si, s in enumerate(segs):
        ei, xi, ki = int(s['entry_i']), int(s['exit_i']), int(s['ki'])
        rg = float(mat[xi, ki] / mat_o[ei, ki] - 1)          # 毛收益：开盘买入价 → 收盘卖出价
        seg_prod *= (1 + rg) * (1 - FEE) * (1 - FEE)         # 精确值，不用页面上的 round2
        if si > 0:                                            # 该次入场漏掉的隔夜（昨收 → 今开）
            gap_prod *= float(mat_o[ei, ki] / mat[ei - 1, ki])
    # 闭合自检：各段连乘 == 期末净值 / 起算净值（起算 = nav[SIGNAL_START-1] = 100）
    seg_expect = float(nav[-1] / nav[SIGNAL_START - 1])
    if segs and segs[-1].get('open_flag'):
        # 末段未平仓：明细里按口径预扣了卖出佣金，而 nav 末日并未卖出 → 这笔佣金要从 expect 里扣掉才对齐
        seg_expect *= (1 - FEE)

    # ---- 品种累计持仓贡献（持有期间） ----
    contrib_curve = np.ones((n, len(CODES)))
    cvec = np.ones(len(CODES))
    held_days = np.zeros(len(CODES), int)
    for t in range(SIGNAL_START, n):
        k = int(hold[t])
        cvec[k] *= mat[t, k] / mat[t - 1, k]
        held_days[k] += 1
        contrib_curve[t] = cvec
    contrib_curve[:SIGNAL_START] = 1.0
    contrib_pct = (contrib_curve - 1) * 100
    contrib_final = {CODES[j]: round(float(contrib_pct[-1, j]), 2) for j in range(len(CODES))}

    # ---- 逐年 / 逐月 ----
    yrs = np.array([int(d[:4]) for d in common])
    yearly = []
    for y in sorted(set(yrs.tolist())):
        ii = np.where(yrs == y)[0]
        if len(ii) < 20 or ii[0] < SIGNAL_START:
            continue
        st = max(ii[0], SIGNAL_START)
        rs = (nav[ii[-1]] / nav[max(st - 1, SIGNAL_START)] - 1) * 100
        re = (ew[ii[-1]] / ew[max(st - 1, MAX_W)] - 1) * 100
        nsw = int(sum(1 for t in range(st, ii[-1] + 1) if hold[t] != hold[t - 1]))
        yearly.append(dict(year=int(y), days=len(ii), s=round(float(rs), 2), ew=round(float(re), 2),
                           ex=round(float(rs - re), 2), switches=nsw))

    yms = [d[:6] for d in common]
    monthly = []
    for ym in sorted(set(yms)):
        ii = np.array([k for k, v in enumerate(yms) if v == ym])
        if len(ii) == 0 or ii[0] <= SIGNAL_START:
            continue
        b = ii[0] - 1
        s_ = (nav[ii[-1]] / nav[b] - 1) * 100
        e_ = (ew[ii[-1]] / ew[b] - 1) * 100
        monthly.append(dict(ym=ym, year=int(ym[:4]), month=int(ym[4:]), days=len(ii),
                            s=round(float(s_), 2), ew=round(float(e_), 2), ex=round(float(s_ - e_), 2)))

    # ---- 绩效 ----
    M = dict(strategy=met(nav), ew=met(ew), base=met(nav_base), v2co=met(nav))
    for j, c in enumerate(CODES):
        etf_nav = 100.0 * mat[:, j] / mat[SIGNAL_START, j]
        M[c] = met(etf_nav)

    # ---- 最新信号（v2co 口径：卖/买分处两个集合竞价） ----
    t = n - 1
    holding_i = int(hold_co[t])        # 当日实际持有（-1 = 今日收盘已卖出、待明日开盘买入）
    pend_i    = int(pend_co[t])        # 若今日已卖出 → 待明日开盘买入的目标
    target_i  = int(top1[t])           # 今日收盘算出的信号
    if holding_i < 0:
        next_i = pend_i if pend_i >= 0 else target_i
        switching = True
        action = f'今日收盘已卖出 → 明日开盘买 {NAMES[CODES[next_i]]}'
    elif target_i != holding_i:
        next_i = target_i
        switching = True
        action = f'今日收盘卖 {NAMES[CODES[holding_i]]} → 明日开盘买 {NAMES[CODES[next_i]]}'
    else:
        next_i = holding_i
        switching = False
        action = f'继续持有 {NAMES[CODES[holding_i]]}'
    mom_tbl = {str(w): [round(float((mat[t][j] / mat[t - w][j] - 1) * 100), 2) for j in range(len(CODES))]
               for w in WINDOWS}
    # 近 20 日涨跌幅：仅人工校验用（主人 2026-09-24），只统计、不参与动量分
    mom20 = [round(float((mat[t][j] / mat[t - 20][j] - 1) * 100), 2) for j in range(len(CODES))]
    accel_tbl = [round(float(((mat[t][j] / mat[t - 21][j] - 1) - (mat[t - 21][j] / mat[t - 42][j] - 1)) * 100), 2)
                 for j in range(len(CODES))]
    # 页面顶部的价格时点（人话）：盘中=实时 tick 价，盘后=当日收盘 tick，退化=日线 bar
    asof = f"{common[-1]} {px_info['ts'][-8:]}" if px_info['live'] else f"{common[-1]}（日线 bar）"
    lbl = (f"{common[-1]} 实时 {px_info['ts'][-8:]}" if px_info['intraday'] else f"{common[-1]} 收盘")
    # px 一律取 mat[t]（＝图表/净值实际使用的那一列价格），保证「页面显示的价格」与「算出来的净值」同源
    cur = dict(date=common[-1], label=lbl, live=bool(px_info['live']),
               mode=px_info['mode'], price_note=px_info['note'], price_asof=asof,
               target=NAMES[CODES[next_i]], target_code=CODES[next_i],
               holding=(NAMES[CODES[holding_i]] if holding_i >= 0 else '现金（今日收盘已卖出）'),
               holding_code=(CODES[holding_i] if holding_i >= 0 else ''),
               holding_i=int(holding_i), pend_i=int(pend_i), signal_i=int(target_i),
               switch_today=bool(switching), action=action,
               px={c: round(float(mat[t][j]), 4) for j, c in enumerate(CODES)},
               scores={CODES[j]: round(float(scores[t][j]), 1) for j in range(len(CODES))},
               mom=mom_tbl, accel=accel_tbl, mom20=mom20)

    dd_s = (nav / np.maximum.accumulate(nav) - 1) * 100              # v2co（主体）
    dd_b = (nav_base / np.maximum.accumulate(nav_base) - 1) * 100    # 基线对照
    dd_e = (ew / np.maximum.accumulate(ew) - 1) * 100                # 等权基准

    return dict(
        code='4ETF', name='4ETF v2co 收卖开买（创业板/纳指/黄金/豆粕）',
        codes=CODES, names=NAMES, colors=COLORS, windows=WINDOWS, accel_w=ACCEL_W,
        dates=common,
        close={c: [round(float(x), 4) for x in mat[:, j]] for j, c in enumerate(CODES)},
        nav=[round(float(x), 4) for x in nav],
        nav_base=[round(float(x), 4) for x in nav_base],
        ew=[round(float(x), 4) for x in ew],
        etf_nav={c: [round(float(x), 4) for x in (100.0 * mat[:, j] / mat[SIGNAL_START, j])]
                 for j, c in enumerate(CODES)},
        hold=[int(x) if i >= SIGNAL_START else -1 for i, x in enumerate(hold)],
        dd=dict(strategy=[round(float(x), 3) for x in dd_s],
                base=[round(float(x), 3) for x in dd_b],
                ew=[round(float(x), 3) for x in dd_e]),
        contrib={CODES[j]: [round(float(x), 3) for x in contrib_pct[:, j]] for j in range(len(CODES))},
        contrib_final=contrib_final,
        contrib_held_days={CODES[j]: int(held_days[j]) for j in range(len(CODES))},
        metrics=M,
        trades=segs, yearly=yearly, monthly=monthly, cur=cur,
        tie=dict(n_l2=int(l2), n_l3=int(l3), total=int(n - SIGNAL_START)),
        n_segs=len(segs), n_trades=int(trades), win_rate=round(wins / max(len(segs), 1) * 100, 1),
        n_trades_base=int(trades_base),
        fee_total_co=round(float(fee_co) * 100, 3),
        # 闭合校验（v2co）：各段连乘 == 期末净值 / 起算净值；gap = 换仓漏掉的隔夜累计
        seg_recon=dict(prod=round(float(seg_prod), 8), expect=round(seg_expect, 8),
                       gap=round((gap_prod - 1) * 100, 4), gap_mult=round(float(gap_prod), 8),
                       n_segs=len(segs), n_trades=int(trades)),
        overnight_gap=round((gap_prod - 1) * 100, 4),
        signal_start=SIGNAL_START,
        params=dict(fee=FEE, sell_slip=SELL_SLIP, accel_w=ACCEL_W, windows=WINDOWS,
                    start=common[0], end=common[-1], days=n,
                    engine='v2co · t日收盘卖 + t+1日开盘买 · 零滑点 · T+1约束（买入日不再决策）',
                    base_note='对照曲线「基线(v2)」= 同数据同信号下 t日收盘卖 + t日收盘买，'
                              '换仓双向佣金万0.6 + 卖出滑点5bp'),
        gen_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        fetch_sec=round(fetch_sec, 2),
        data_source=DATASRC,
        live=bool(px_info['live']),
        live_time=(px_info['ts'][-8:] if px_info['live'] else None),
        price_source=dict(mode=px_info['mode'], live=bool(px_info['live']), ts=px_info['ts'],
                          note=px_info['note'], tick_tag=px_info['tick_tag'],
                          bar_last=px_info['bar_last'], per_code=px_info['per_code']),
        asof=asof,
    )

# ================= 简化输出：4etf.html（数据内嵌，ECharts 画净值/回撤） =================
TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>4ETF v2co 净值 · 回撤</title>
<style>
:root{--bg:#0f1420;--panel:#171d2b;--line:#2a3446;--tx:#d8dee9;--tx2:#8b95a8;--gold:#f5c542}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;font-size:13px}
.wrap{max-width:1200px;margin:0 auto;padding:16px 18px 60px}
h1{font-size:18px;margin:0 0 4px;font-weight:600}
.sub{color:var(--tx2);font-size:12px;line-height:1.9;margin-bottom:14px}
.chartbox{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 6px;margin-bottom:14px}
.ct{font-size:13px;font-weight:600;padding:2px 10px 8px}
.metric{display:inline-block;background:#1c2433;border:1px solid var(--line);border-radius:7px;
padding:8px 12px;margin:0 8px 8px 0}
.metric .k{color:var(--tx2);font-size:11px}.metric .v{font-size:16px;font-weight:600;color:var(--gold)}
</style></head><body><div class="wrap">
<h1>4ETF <span style="color:var(--gold)">v2co 收卖开买</span> · 净值 / 回撤（腾讯数据源）</h1>
<div class="sub" id="sub">加载中…</div>
<div id="metrics"></div>
<div class="chartbox"><div class="ct">净值曲线（对数）</div><div id="nav" style="height:400px"></div></div>
<div class="chartbox"><div class="ct">回撤曲线（%）</div><div id="dd" style="height:300px"></div></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
<script>
var D = __DATA__;
function line(el, series, log, yFmt){
  var c = echarts.init(document.getElementById(el));
  c.setOption({backgroundColor:'transparent',animation:false,
    tooltip:{trigger:'axis',backgroundColor:'rgba(20,26,38,.95)',borderColor:'#2a3446',
      textStyle:{color:'#d8dee9',fontSize:12},
      valueFormatter:function(v){return v==null?'-':yFmt(v);}},
    legend:{data:series.map(function(s){return s.name;}),textStyle:{color:'#8b95a8',fontSize:11},top:0,right:8},
    grid:{left:56,right:20,top:30,bottom:24},
    xAxis:{type:'category',data:D.dates,boundaryGap:false,
      axisLine:{lineStyle:{color:'#2a3446'}},axisLabel:{color:'#8b95a8',fontSize:10},
      splitLine:{show:false},min:'dataMin',max:'dataMax'},
    yAxis:{type:log?'log':'value',position:'right',splitLine:{lineStyle:{color:'#212a3a'}},
      axisLine:{show:false},axisLabel:{color:'#8b95a8',fontSize:10}},
    series:series});
  window.addEventListener('resize',function(){c.resize();});
}
var m=D.metrics.strategy;
document.getElementById('sub').innerHTML =
  '数据区间 <b>'+D.dates[0]+' ~ '+D.dates[D.dates.length-1]+'</b>（'+D.dates.length+' 个交易日）　'
  +'数据源 '+D.data_source+'　生成 '+D.gen_time+'　每 10 分钟自动更新（GitHub Actions）';
document.getElementById('metrics').innerHTML =
  '<span class="metric"><span class="k">v2co 年化</span><br><span class="v">'+m.ann.toFixed(2)+'%</span></span>'
 +'<span class="metric"><span class="k">夏普</span><br><span class="v">'+m.sharpe.toFixed(2)+'</span></span>'
 +'<span class="metric"><span class="k">最大回撤</span><br><span class="v">'+m.mdd.toFixed(2)+'%</span></span>'
 +'<span class="metric"><span class="k">总收益</span><br><span class="v">'+m.total.toFixed(1)+'%</span></span>'
 +'<span class="metric"><span class="k">期末净值</span><br><span class="v">'+D.nav[D.nav.length-1].toFixed(2)+'</span></span>';
line('nav',[
  {name:'v2co 收卖开买',type:'line',data:D.nav,symbol:'none',lineStyle:{width:2.4,color:'#f5c542'}},
  {name:'基线 v2（收盘买）',type:'line',data:D.nav_base,symbol:'none',lineStyle:{width:1.6,color:'#4dd0e1'}},
  {name:'等权基准',type:'line',data:D.ew,symbol:'none',lineStyle:{width:1.3,color:'#6b7a99'}}
],true,function(v){return v.toFixed(2);});
line('dd',[
  {name:'v2co 回撤',type:'line',data:D.dd.strategy,symbol:'none',lineStyle:{width:1.8,color:'#ef4b4b'}},
  {name:'基线 v2 回撤',type:'line',data:D.dd.base,symbol:'none',lineStyle:{width:1.4,color:'#b07cf0'}},
  {name:'等权回撤',type:'line',data:D.dd.ew,symbol:'none',lineStyle:{width:1.2,color:'#6b7a99'}}
],false,function(v){return v.toFixed(1)+'%';});
</script></body></html>"""


def render_html(data):
    d = dict(
        code=data['code'], name=data['name'], dates=data['dates'],
        nav=data['nav'], nav_base=data['nav_base'], ew=data['ew'],
        dd=data['dd'], metrics=data['metrics'], cur=data['cur'],
        asof=data['asof'], gen_time=data['gen_time'], data_source=data['data_source'],
        params=data['params'],
    )
    payload = json.dumps(d, ensure_ascii=False, separators=(',', ':'))
    html = TPL.replace('__DATA__', payload)
    return html


def main():
    t0 = time.time()
    data = build_data()
    html = render_html(data)
    with open('4etf.html', 'w', encoding='utf-8') as f:
        f.write(html)
    m = data['metrics']['strategy']
    print('OK  %s ~ %s  n=%d  gen=%s  fetch=%.1fs  html=%d 字节'
          % (data['dates'][0], data['dates'][-1], len(data['dates']),
             data['gen_time'], time.time() - t0, len(html)), flush=True)
    print('    v2co: 年化 %.2f%%  夏普 %.2f  回撤 %.2f%%  总收益 %.1f%%  期末净值 %.2f'
          % (m['ann'], m['sharpe'], m['mdd'], m['total'], data['nav'][-1]), flush=True)


if __name__ == '__main__':
    main()

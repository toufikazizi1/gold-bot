"""مؤشرات RSI / MACD / CCI + مؤشر سعر الإغلاق. الشموع: قائمة dict فيها o,h,l,c."""


def ema(values, n):
    k = 2.0 / (n + 1)
    out, prev = [], None
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + g / l)


def macd_hist(closes):
    """يرجع (الهيستوغرام الحالي، السابق) أو None."""
    if len(closes) < 35:
        return None
    e12, e26 = ema(closes, 12), ema(closes, 26)
    m = [a - b for a, b in zip(e12, e26)]
    s = ema(m, 9)
    return m[-1] - s[-1], m[-2] - s[-2]


def cci(candles, n=20):
    if len(candles) < n:
        return None
    tp = [(c['h'] + c['l'] + c['c']) / 3.0 for c in candles][-n:]
    sma = sum(tp) / n
    md = sum(abs(x - sma) for x in tp) / n
    if md == 0:
        return 0.0
    return (tp[-1] - sma) / (0.015 * md)


def signal_for(ind, candles):
    """'up' شراء، 'down' بيع، None لا إشارة. candles = الشموع المغلقة فقط."""
    closes = [c['c'] for c in candles]
    if ind == 'RSI':
        v = rsi(closes)
        return None if v is None else 'up' if v < 30 else 'down' if v > 70 else None
    if ind == 'MACD':
        h = macd_hist(closes)
        if h is None:
            return None
        cur, prev = h
        return 'up' if prev <= 0 < cur else 'down' if prev >= 0 > cur else None
    if ind == 'CCI':
        v = cci(candles)
        return None if v is None else 'up' if v < -100 else 'down' if v > 100 else None
    if ind == 'CLOSE':  # ثلاث شموع متتالية في نفس الاتجاه
        last = candles[-3:]
        if len(last) < 3:
            return None
        if all(c['c'] > c['o'] for c in last):
            return 'up'
        if all(c['c'] < c['o'] for c in last):
            return 'down'
    return None

"""اختبار الجسر بـ MT5 وهمي: python test_bridge.py"""
import json
import threading
import time
import urllib.request
from types import SimpleNamespace as NS

import bridge
from bridge import BotError, Engine, MAGIC


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    TIMEFRAME_M1 = 1
    DEAL_ENTRY_OUT = 1
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1

    def __init__(self):
        self.t = 1_700_000_000
        self.bid, self.spread = 4000.0, 0.2
        self.positions, self.deals, self.requests = [], [], []
        self.symbols = {'XAUUSDm': NS(digits=2, point=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                                      trade_contract_size=100.0, trade_stops_level=10, filling_mode=2, visible=True)}
        self.fail_retcode = None
        self.rates = []
        self.next_ticket = 1000
        self.ok_login = 472954969
        self.algo = True

    # --- اتصال ---
    def initialize(self, login=None, password=None, server=None):
        if login is None and password is None and server is None:
            return True  # اتصال بالطرفية المفتوحة بدون إعادة تسجيل دخول
        return login == self.ok_login and password == 'pw' and server == 'Exness-MT5Trial16'

    def last_error(self):
        return (-6, 'Authorization failed')

    def shutdown(self):
        pass

    def terminal_info(self):
        return NS(connected=True, trade_allowed=self.algo)

    def account_info(self):
        return NS(login=self.ok_login, server='Exness-MT5Trial16', name='Demo', balance=5000.0, equity=5000.0,
                  currency='USD', leverage=2000, trade_mode=0)

    def symbol_info(self, s):
        return self.symbols.get(s)

    def symbol_select(self, s, v):
        return True

    def symbol_info_tick(self, s):
        return NS(bid=self.bid, ask=round(self.bid + self.spread, 2), time=self.t)

    def copy_rates_from_pos(self, s, tf, pos, n):
        return self.rates[-n:]

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def history_deals_get(self, a, b):
        return self.deals

    # --- أوامر ---
    def order_send(self, req):
        self.requests.append(req)
        if self.fail_retcode:
            return NS(retcode=self.fail_retcode, comment='fail')
        if req['action'] == self.TRADE_ACTION_SLTP:
            for p in self.positions:
                if p.ticket == req['position']:
                    p.sl = req['sl']
            return NS(retcode=10009, comment='ok')
        if 'position' in req:
            for p in list(self.positions):
                if p.ticket == req['position']:
                    self._close(p, req['price'], 3)
            return NS(retcode=10009, comment='ok')
        self.next_ticket += 1
        buy = req['type'] == self.ORDER_TYPE_BUY
        self.positions.append(NS(ticket=self.next_ticket, symbol=req['symbol'], type=0 if buy else 1, volume=req['volume'],
                                 price_open=req['price'], sl=req['sl'], tp=req['tp'], profit=0.0, time=self.t, magic=MAGIC))
        return NS(retcode=10009, comment='ok')

    def _close(self, p, price, reason):
        buy = p.type == 0
        profit = ((price - p.price_open) if buy else (p.price_open - price)) * p.volume * 100.0
        self.deals.append(NS(magic=MAGIC, entry=1, time=self.t, type=1 if buy else 0, volume=p.volume, price=price,
                             profit=profit, commission=0.0, swap=0.0, fee=0.0, reason=reason))
        self.positions.remove(p)

    def move(self, bid):
        """يحرك السعر ويحدّث الربح ويفعّل SL/TP تلقائيا كباينانس/MT5."""
        self.bid = bid
        ask = round(bid + self.spread, 2)
        for p in list(self.positions):
            buy = p.type == 0
            p.profit = ((bid - p.price_open) if buy else (p.price_open - ask)) * p.volume * 100.0
            if buy and p.sl and bid <= p.sl:
                self._close(p, p.sl, 4)
            elif buy and p.tp and bid >= p.tp:
                self._close(p, p.tp, 5)
            elif not buy and p.sl and ask >= p.sl:
                self._close(p, p.sl, 4)
            elif not buy and p.tp and ask <= p.tp:
                self._close(p, p.tp, 5)

    def add_candle(self, o, h, l, c):
        self.t += 60
        self.rates.append({'time': self.t, 'open': o, 'high': h, 'low': l, 'close': c})


def fresh():
    m = FakeMT5()
    e = Engine(m)
    e.connect(472954969, 'pw', 'Exness-MT5Trial16')
    return m, e


def raises(fn, text):
    try:
        fn()
    except BotError as ex:
        assert text in str(ex), '%s not in %s' % (text, ex)
        return
    raise AssertionError('no error, expected ' + text)


def test_connect():
    m = FakeMT5()
    e = Engine(m)
    raises(lambda: e.connect('abc', 'pw', 'S'), 'رقم الحساب')
    raises(lambda: e.connect(1, 'x', 'S'), 'فشل الاتصال')
    raises(lambda: e.open_position('buy'), 'غير متصل')
    e.connect(472954969, 'pw', 'Exness-MT5Trial16')
    assert e.symbol == 'XAUUSDm' and e.connected
    m2 = FakeMT5(); m2.symbols = {}
    raises(lambda: Engine(m2).connect(472954969, 'pw', 'Exness-MT5Trial16'), 'رمز الذهب')
    m3 = FakeMT5(); m3.algo = False
    e3 = Engine(m3); e3.connect(472954969, 'pw', 'Exness-MT5Trial16')
    assert any('Algo Trading' in l['m'] for l in e3.logs)
    # بدون كلمة سر: يتصل بـ MT5 المفتوح
    m4 = FakeMT5(); e4 = Engine(m4); e4.connect(472954969, '', 'Exness-MT5Trial16')
    assert e4.connected
    raises(lambda: Engine(FakeMT5()).connect(111, '', 'Exness-MT5Trial16'), 'مسجل على حساب')
    raises(lambda: Engine(FakeMT5()).connect(472954969, '', 'Other-Server'), 'خادم MT5')


def test_open_and_sl_tp():
    m, e = fresh()
    e.open_position('buy', 0.01)
    r = m.requests[-1]
    ask = 4000.2
    assert r['volume'] == 0.01 and r['type'] == 0 and r['magic'] == MAGIC and r['type_filling'] == 1
    assert abs(r['sl'] - (ask - 3.0)) < 0.011 and abs(r['tp'] - (ask + 6.0)) < 0.011, r
    raises(lambda: e.open_position('buy'), 'فيه صفقة مفتوحة')
    m.positions.clear()
    e.open_position('sell', 0.01)
    r = m.requests[-1]
    assert r['type'] == 1 and abs(r['sl'] - (4000.0 + 3.0)) < 0.011 and abs(r['tp'] - (4000.0 - 6.0)) < 0.011, r
    m.positions.clear()
    raises(lambda: e.open_position('buy', 0.5), 'أكبر من الحد')
    raises(lambda: e.open_position('buy', 0.001), 'أقل من الحد')
    e.set_bot({'sl': 0.05})
    raises(lambda: e.open_position('buy', 0.01), 'قريب بزاف')
    e.set_bot({'sl': 3})
    m.fail_retcode = 10027
    raises(lambda: e.open_position('buy', 0.01), 'Algo Trading')


def test_trailing():
    m, e = fresh()
    e.set_bot({'sl': 3, 'tp': 20, 'trail': 2})
    e.open_position('buy', 0.01)
    p = m.positions[0]
    entry = p.price_open
    assert abs(p.sl - (entry - 3)) < 0.011
    m.move(entry + 4)            # ربح 4$ => الوقف عند +2$
    e.tick()
    assert abs(p.sl - (entry + 2)) < 0.011, p.sl
    n = len([r for r in m.requests if r['action'] == 6])
    m.move(entry + 4.1)          # تحسن صغير ما يحركش
    e.tick()
    assert len([r for r in m.requests if r['action'] == 6]) == n
    m.move(entry + 7)            # ربح 7$ => وقف +5$
    e.tick()
    assert abs(p.sl - (entry + 5)) < 0.011, p.sl
    m.move(entry + 4)            # رجع تحت الوقف => MT5 سكّر بـ SL
    assert not m.positions and m.deals[-1].reason == 4 and m.deals[-1].profit > 4
    st = e.state()
    assert st['trades'][0]['reason'] == 'SL' and st['trades'][0]['profit'] > 4 and 'position' not in st


def test_time_exit_and_crossed():
    m, e = fresh()
    e.open_position('buy', 0.01, max_min=1)
    m.t += 61
    e.tick()
    assert not m.positions and 'انتهى الوقت' in e.logs[0]['m'] + e.logs[1]['m']
    m.positions.clear()
    e.set_bot({'trail': 1})
    e.open_position('buy', 0.01, max_min=0)
    entry = m.positions[0].price_open
    m.move(entry + 3)
    p = m.positions[0]
    p.sl = entry - 3           # نتصور أن الوقف ما تحركش
    m.bid = entry + 0.4        # السعر نزل تحت الوقف الجديد (+2) قبل ما نحركو
    p.profit = 3.0
    e.best[p.ticket] = 3.0
    e.tick()
    assert not m.positions, 'يسكر بالسوق إذا السعر عدى الوقف الجديد'


def test_signal_flow():
    m, e = fresh()
    price = 4200.0
    for i in range(60):
        m.add_candle(price, price + 0.1, price - 0.3, price - 2)
        price -= 2
    m.bid = price
    e.set_bot({'ind': 'RSI', 'on': True})
    e.tick()
    assert not m.positions, 'ما يدخلش على إشارة قديمة'
    m.add_candle(price, price + 0.1, price - 0.3, price - 2)   # شمعة جديدة مغلقة (والجارية جاية)
    m.add_candle(price - 2, price - 1.9, price - 2.5, price - 2.2)  # الشمعة الجارية
    e.tick()
    assert m.positions and m.positions[0].type == 0, 'RSI منخفض => شراء'
    assert e.bot['on']


def test_day_limits():
    m, e = fresh()
    m.deals.append(NS(magic=MAGIC, entry=1, time=m.t, type=1, volume=0.01, price=4000, profit=-11.0, commission=0, swap=0, fee=0, reason=4))
    e.set_bot({'on': True})
    e.tick()
    assert not e.bot['on'] and any('حد الخسارة' in l['m'] for l in e.logs)
    m2, e2 = fresh()
    e2.set_bot({'dayProfit': 5})
    m2.deals.append(NS(magic=MAGIC, entry=1, time=m2.t, type=1, volume=0.01, price=4000, profit=6.0, commission=0, swap=0, fee=0, reason=5))
    e2.set_bot({'on': True}); e2.tick()
    assert not e2.bot['on'] and any('حد الربح' in l['m'] for l in e2.logs)
    # صفقات بمجيك آخر تتجاهل
    m3, e3 = fresh()
    m3.deals.append(NS(magic=1, entry=1, time=m3.t, type=1, volume=1, price=4000, profit=-500.0, commission=0, swap=0, fee=0, reason=0))
    assert e3.day_profit() == 0


def test_config_validation():
    m, e = fresh()
    raises(lambda: e.set_bot({'ind': 'X'}), 'مؤشر')
    raises(lambda: e.set_bot({'lot': 1}), 'الحجم')
    raises(lambda: e.set_bot({'sl': 0}), 'وقف')
    raises(lambda: e.set_bot({'tp': -1}), 'قيمة')
    raises(lambda: e.set_bot({'dayLoss': 0}), 'حد')
    m2 = FakeMT5()
    raises(lambda: Engine(m2).set_bot({'on': True}), 'غير متصل')


def test_http():
    m = FakeMT5()
    e = Engine(m)
    srv = bridge.serve(e, 'tok', '127.0.0.1', 0)
    port = srv.server_address[1]
    base = 'http://127.0.0.1:%d' % port

    def call(path, body=None, token='tok'):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'x-token': token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())
    assert call('/api/state', token='bad')[0] == 401
    assert call('/api/state') == (200, call('/api/state')[1]) and call('/api/state')[1]['connected'] is False
    code, j = call('/api/connect', {'login': 472954969, 'password': 'nope', 'server': 'Exness-MT5Trial16'})
    assert code == 400 and 'فشل' in j['error']
    assert call('/api/connect', {'login': 472954969, 'password': 'pw', 'server': 'Exness-MT5Trial16'})[0] == 200
    m.add_candle(4000, 4001, 3999, 4000.5)
    st = call('/api/state')[1]
    assert st['connected'] and st['account']['demo'] and st['price']['ask'] == 4000.2 and st['symbol'] == 'XAUUSDm'
    assert call('/api/order', {'side': 'buy', 'lot': 0.01, 'maxMin': 5})[0] == 200
    st = call('/api/state')[1]
    assert st['position']['side'] == 'buy' and st['position']['maxMin'] == 5
    assert call('/api/order', {'side': 'zzz'})[0] == 400
    assert call('/api/close', {})[0] == 200
    assert 'position' not in call('/api/state')[1]
    assert call('/api/candles?n=20')[1]['candles'][0][4] == 4000.5
    assert call('/api/bot', {'on': True, 'ind': 'MACD'})[0] == 200 and call('/api/state')[1]['bot']['ind'] == 'MACD'
    assert call('/api/disconnect', {})[0] == 200 and call('/api/state')[1]['connected'] is False
    srv.shutdown()


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('ALL_TESTS_PASS')

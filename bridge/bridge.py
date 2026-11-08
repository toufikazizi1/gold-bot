#!/usr/bin/env python3
"""جسر Exness MT5 + محرك بوت الذهب.

يخدم على ويندوز مع MetaTrader 5 مفتوح. التطبيق في الهاتف يتصل بهذا الجسر (نفس الواي فاي)،
والجسر هو اللي يفتح الصفقات ويحرك الوقف في حسابك على MT5.
الوقف والهدف يتحطو داخل MT5 نفسه، يعني الصفقة محمية حتى إذا طفى الجسر.
"""
import json
import math
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import indicators

MAGIC = 777001
GOLD_CANDIDATES = ['XAUUSDm', 'XAUUSD', 'XAUUSDc', 'XAUUSD.a', 'XAUUSDz', 'XAUUSD#', 'GOLD']
MAX_LOT = float(os.environ.get('MAX_LOT', '0.05'))  # حماية: أقصى حجم للصفقة


class BotError(Exception):
    pass


class Engine:
    def __init__(self, mt5):
        self.mt5 = mt5
        self.lock = threading.RLock()
        self.connected = False
        self.symbol = None
        self.bot = dict(on=False, ind='RSI', lot=0.01, sl=3.0, tp=6.0, trail=2.0, maxMin=0,
                        dayProfit=0.0, dayLoss=10.0, strategy='optimal')
        self.logs = []
        self.best = {}
        self.last_candle = 0
        self.cur_max_min = 0

    # ---------- أدوات ----------
    def log(self, msg):
        self.logs.insert(0, {'t': int(time.time()), 'm': msg})
        del self.logs[80:]

    def _need(self):
        if not self.connected:
            raise BotError('غير متصل بحساب MT5: اربط الحساب من تبويب الحساب')

    def _check(self, res, what):
        m = self.mt5
        if res is None:
            raise BotError('MT5 ما جاوبش على %s: %s' % (what, m.last_error()))
        if res.retcode != m.TRADE_RETCODE_DONE:
            hint = ''
            if res.retcode == 10027:
                hint = ' — فعّل زر Algo Trading في MT5'
            elif res.retcode in (10018, 10019):
                hint = ' — السوق مسكّر ولا الرصيد ما يكفيش'
            elif res.retcode == 10030:
                hint = ' — نوع التنفيذ غير مدعوم'
            raise BotError('MT5 رفض %s (%s): %s%s' % (what, res.retcode, res.comment, hint))

    def _filling(self, si):
        m, fm = self.mt5, getattr(si, 'filling_mode', 0)
        if fm & 2:
            return m.ORDER_FILLING_IOC
        if fm & 1:
            return m.ORDER_FILLING_FOK
        return m.ORDER_FILLING_RETURN

    def _norm_lot(self, si, lot):
        lot = float(lot)
        step = si.volume_step or 0.01
        lot = round(math.floor(lot / step + 1e-9) * step, 8)
        if lot < si.volume_min:
            raise BotError('الحجم أقل من الحد الأدنى (%s)' % si.volume_min)
        if lot > si.volume_max or lot > MAX_LOT:
            raise BotError('الحجم أكبر من الحد المسموح (%s)' % min(si.volume_max, MAX_LOT))
        return lot

    def positions(self):
        m = self.mt5
        if not self.symbol:
            return []
        return [p for p in (m.positions_get(symbol=self.symbol) or []) if p.magic == MAGIC]

    def resolve_symbol(self, pref=None):
        m = self.mt5
        for c in ([pref] if pref else GOLD_CANDIDATES):
            if m.symbol_info(c) is not None:
                m.symbol_select(c, True)
                return c
        raise BotError('ما لقيتش رمز الذهب في حسابك (جربت: %s)' % ', '.join([pref] if pref else GOLD_CANDIDATES))

    # ---------- الاتصال ----------
    def connect(self, login, password, server, symbol=None):
        with self.lock:
            m = self.mt5
            try:
                login = int(str(login).strip())
            except ValueError:
                raise BotError('رقم الحساب غير صالح')
            if not password or not str(server).strip():
                raise BotError('لازم تدخل كلمة المرور واسم الخادم')
            if not m.initialize(login=login, password=password, server=str(server).strip()):
                raise BotError('فشل الاتصال بـ MT5: %s' % (m.last_error(),))
            info = m.account_info()
            if info is None or info.login != login:
                raise BotError('ما قدرتش نتسجل في الحساب %s: %s' % (login, m.last_error()))
            self.symbol = self.resolve_symbol(symbol or None)
            self.connected = True
            self.best = {}
            self.last_candle = 0
            ti = m.terminal_info()
            self.log('تم الربط: حساب %s على %s، الرمز %s' % (login, server, self.symbol))
            if ti is not None and not getattr(ti, 'trade_allowed', True):
                self.log('⚠️ زر Algo Trading مطفي في MT5: فعّلو باش البوت يقدر يفتح صفقات')

    def disconnect(self):
        with self.lock:
            self.bot['on'] = False
            self.connected = False
            try:
                self.mt5.shutdown()
            except Exception:
                pass
            self.log('تم قطع الاتصال')

    def set_symbol(self, symbol):
        with self.lock:
            self._need()
            if self.positions():
                raise BotError('سكّر الصفقة المفتوحة قبل تغيير الرمز')
            self.symbol = self.resolve_symbol(symbol)
            self.last_candle = 0
            self.log('الرمز: ' + self.symbol)

    # ---------- إعدادات البوت ----------
    def set_bot(self, cfg):
        with self.lock:
            nb = dict(self.bot)
            for k in ('lot', 'sl', 'tp', 'trail', 'dayProfit', 'dayLoss'):
                if k in cfg:
                    try:
                        v = float(cfg[k])
                    except (TypeError, ValueError):
                        raise BotError('قيمة غير صالحة: %s' % k)
                    if not math.isfinite(v) or v < 0:
                        raise BotError('قيمة غير صالحة: %s' % k)
                    nb[k] = v
            if 'maxMin' in cfg:
                nb['maxMin'] = max(0, int(float(cfg['maxMin'])))
            if 'ind' in cfg:
                if cfg['ind'] not in ('RSI', 'MACD', 'CCI', 'CLOSE'):
                    raise BotError('مؤشر غير صالح')
                nb['ind'] = cfg['ind']
            if 'strategy' in cfg:
                nb['strategy'] = str(cfg['strategy'])[:20]
            if nb['lot'] <= 0 or nb['lot'] > MAX_LOT:
                raise BotError('الحجم لازم يكون بين 0 و %s' % MAX_LOT)
            if nb['sl'] <= 0:
                raise BotError('وقف الخسارة لازم يكون أكبر من 0')
            if nb['dayLoss'] <= 0:
                raise BotError('حد الخسارة اليومي لازم يكون أكبر من 0')
            if 'on' in cfg:
                want = bool(cfg['on'])
                if want and not self.bot['on']:
                    self._need()
                    self._mark_candle()
                    self.log('تشغيل البوت: %s على %s' % (nb['ind'], self.symbol))
                if not want and self.bot['on']:
                    self.log('تم إيقاف البوت')
                nb['on'] = want
            self.bot = nb

    def _mark_candle(self):
        m = self.mt5
        rates = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_M1, 0, 3)
        if rates is not None and len(rates) >= 2:
            self.last_candle = int(rates[-2]['time'])  # ننتظر الشمعة الجاية، ما ندخلش على إشارة قديمة

    # ---------- الصفقات ----------
    def open_position(self, side, lot=None, src='يدوي', max_min=None):
        with self.lock:
            self._need()
            m, b, sym = self.mt5, self.bot, self.symbol
            if self.positions():
                raise BotError('فيه صفقة مفتوحة')
            si, tick = m.symbol_info(sym), m.symbol_info_tick(sym)
            if si is None or tick is None:
                raise BotError('ما كاينش سعر للرمز ' + str(sym))
            lot = self._norm_lot(si, lot if lot is not None else b['lot'])
            per = lot * (si.trade_contract_size or 100.0)
            buy = side == 'buy'
            price = tick.ask if buy else tick.bid
            dsl = b['sl'] / per
            dtp = b['tp'] / per if b['tp'] > 0 else 0.0
            min_d = (getattr(si, 'trade_stops_level', 0) or 0) * si.point
            if dsl < min_d:
                raise BotError('الوقف قريب بزاف من السعر: أقل مسافة %.2f (زيد SL ولا نقص الحجم)' % min_d)
            if dtp and dtp < min_d:
                dtp = 0.0
                self.log('الهدف قريب بزاف من السعر فما تحطش')
            d = si.digits
            sl = round(price - dsl if buy else price + dsl, d)
            tp = round(price + dtp if buy else price - dtp, d) if dtp else 0.0
            req = {
                'action': m.TRADE_ACTION_DEAL, 'symbol': sym, 'volume': lot,
                'type': m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL, 'price': price,
                'sl': sl, 'tp': tp, 'deviation': 30, 'magic': MAGIC, 'comment': 'goldbot',
                'type_time': m.ORDER_TIME_GTC, 'type_filling': self._filling(si),
            }
            self._check(m.order_send(req), 'فتح الصفقة')
            self.cur_max_min = b['maxMin'] if max_min is None else int(max_min)
            self.log('فتحت %s %s حجم %s بسعر %s، وقف %s هدف %s (%s)' % ('شراء' if buy else 'بيع', sym, lot, price, sl, tp or '-', src))

    def close_position(self, reason='يدوي'):
        with self.lock:
            self._need()
            ps = self.positions()
            if not ps:
                raise BotError('ما كاينش صفقة مفتوحة')
            self._close(ps[0], reason)

    def _close(self, p, reason):
        m = self.mt5
        si, tick = m.symbol_info(p.symbol), m.symbol_info_tick(p.symbol)
        if tick is None:
            raise BotError('ما كاينش سعر لإغلاق الصفقة')
        buy = p.type == m.POSITION_TYPE_BUY
        req = {
            'action': m.TRADE_ACTION_DEAL, 'symbol': p.symbol, 'volume': p.volume,
            'type': m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, 'position': p.ticket,
            'price': tick.bid if buy else tick.ask, 'deviation': 30, 'magic': MAGIC,
            'comment': 'goldbot close', 'type_time': m.ORDER_TIME_GTC, 'type_filling': self._filling(si),
        }
        self._check(m.order_send(req), 'إغلاق الصفقة')
        self.best.pop(p.ticket, None)
        self.log('سكّرت الصفقة (%s) ربح %.2f$' % (reason, p.profit))

    # ---------- الحلقة ----------
    def tick(self):
        with self.lock:
            if not self.connected:
                return
            try:
                ti = self.mt5.terminal_info()
                if ti is not None and not getattr(ti, 'connected', True):
                    return
                self._tick()
            except BotError as e:
                self.log('خطأ: %s' % e)
            except Exception as e:  # لا نوقف الحلقة
                self.log('خطأ غير متوقع: %r' % (e,))

    def _tick(self):
        ps = self.positions()
        if ps:
            self._manage(ps[0])
        self._day_limits()
        if self.bot['on'] and not self.positions():
            self._signal_step()

    def _manage(self, p):
        m, b = self.mt5, self.bot
        si, tick = m.symbol_info(p.symbol), m.symbol_info_tick(p.symbol)
        if si is None or tick is None:
            return
        per = p.volume * (si.trade_contract_size or 100.0)
        buy = p.type == m.POSITION_TYPE_BUY
        profit = p.profit
        best = max(self.best.get(p.ticket, 0.0), profit)
        self.best[p.ticket] = best
        if self.cur_max_min > 0 and tick.time - p.time >= self.cur_max_min * 60:
            return self._close(p, 'انتهى الوقت')
        stop = -b['sl']
        if b['trail'] > 0 and best >= b['trail']:
            stop = max(stop, best - b['trail'])
        new_sl = round(p.price_open + (stop / per if buy else -stop / per), si.digits)
        cur = p.sl
        thresh = max(b['trail'] * 0.25 / per, si.point * 5)
        better = (cur == 0 or new_sl > cur + thresh) if buy else (cur == 0 or new_sl < cur - thresh)
        if not better:
            return
        now = tick.bid if buy else tick.ask
        min_d = (getattr(si, 'trade_stops_level', 0) or 0) * si.point
        crossed = now <= new_sl if buy else now >= new_sl
        if crossed:
            return self._close(p, 'وقف متحرك')
        if (now - new_sl if buy else new_sl - now) < min_d:
            return
        req = {'action': m.TRADE_ACTION_SLTP, 'position': p.ticket, 'symbol': p.symbol, 'sl': new_sl,
               'tp': p.tp, 'magic': MAGIC}
        try:
            self._check(m.order_send(req), 'تحريك الوقف')
            self.log('رفعت الوقف إلى %s' % new_sl)
        except BotError as e:
            self.log('ما قدرتش نحرك الوقف: %s' % e)

    def _server_now(self):
        t = self.mt5.symbol_info_tick(self.symbol)
        return int(t.time) if t is not None else int(time.time())

    def _deals(self, since):
        m = self.mt5
        now = self._server_now()
        raw = m.history_deals_get(since - 86400, now + 86400) or []
        return [d for d in raw if d.magic == MAGIC and d.entry == m.DEAL_ENTRY_OUT and d.time >= since]

    @staticmethod
    def _net(d):
        return d.profit + getattr(d, 'commission', 0) + getattr(d, 'swap', 0) + getattr(d, 'fee', 0)

    def day_profit(self):
        now = self._server_now()
        return round(sum(self._net(d) for d in self._deals(now - now % 86400)), 2)

    def _day_limits(self):
        if not self.bot['on']:
            return
        dp, b = self.day_profit(), self.bot
        if b['dayProfit'] > 0 and dp >= b['dayProfit']:
            b['on'] = False
            self.log('تم إيقاف البوت: بلغت حد الربح اليومي (%.2f$)' % dp)
        elif b['dayLoss'] > 0 and dp <= -b['dayLoss']:
            b['on'] = False
            self.log('تم إيقاف البوت: بلغت حد الخسارة اليومي (%.2f$)' % dp)

    def _signal_step(self):
        m = self.mt5
        rates = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_M1, 0, 120)
        if rates is None or len(rates) < 40:
            return
        closed = rates[:-1]
        t = int(closed[-1]['time'])
        if t == self.last_candle:
            return
        self.last_candle = t
        candles = [{'o': float(r['open']), 'h': float(r['high']), 'l': float(r['low']), 'c': float(r['close'])} for r in closed]
        d = indicators.signal_for(self.bot['ind'], candles)
        if d:
            self.log('إشارة %s: %s' % (self.bot['ind'], 'شراء' if d == 'up' else 'بيع'))
            self.open_position('buy' if d == 'up' else 'sell', None, 'بوت')

    # ---------- الحالة ----------
    def candles(self, n=70):
        with self.lock:
            self._need()
            m = self.mt5
            rates = m.copy_rates_from_pos(self.symbol, m.TIMEFRAME_M1, 0, max(10, min(int(n), 300)))
            if rates is None:
                return []
            return [[int(r['time']), float(r['open']), float(r['high']), float(r['low']), float(r['close'])] for r in rates]

    def state(self):
        with self.lock:
            st = {'connected': self.connected, 'bot': dict(self.bot), 'log': self.logs[:30], 'symbol': self.symbol,
                  'maxLot': MAX_LOT}
            if not self.connected:
                return st
            m = self.mt5
            info, si, tick = m.account_info(), m.symbol_info(self.symbol), m.symbol_info_tick(self.symbol)
            if info is not None:
                st['account'] = {'login': info.login, 'server': info.server, 'name': getattr(info, 'name', ''),
                                 'balance': info.balance, 'equity': info.equity, 'currency': info.currency,
                                 'leverage': info.leverage, 'demo': info.trade_mode == 0}
            if si is not None:
                st['spec'] = {'digits': si.digits, 'lotMin': si.volume_min, 'lotStep': si.volume_step,
                              'contract': si.trade_contract_size, 'stopsLevel': getattr(si, 'trade_stops_level', 0),
                              'point': si.point}
            if tick is not None:
                st['price'] = {'bid': tick.bid, 'ask': tick.ask, 'time': int(tick.time)}
            ps = self.positions()
            if ps:
                p = ps[0]
                st['position'] = {'ticket': p.ticket, 'side': 'buy' if p.type == m.POSITION_TYPE_BUY else 'sell',
                                  'lot': p.volume, 'entry': p.price_open, 'sl': p.sl, 'tp': p.tp, 'profit': round(p.profit, 2),
                                  'best': round(self.best.get(p.ticket, 0.0), 2), 'time': int(p.time),
                                  'maxMin': self.cur_max_min}
            now = self._server_now()
            st['dayProfit'] = self.day_profit()
            deals = sorted(self._deals(now - 7 * 86400), key=lambda d: d.time, reverse=True)[:20]
            st['trades'] = [{'time': int(d.time), 'side': 'buy' if d.type == m.DEAL_TYPE_SELL else 'sell', 'lot': d.volume,
                             'price': d.price, 'profit': round(self._net(d), 2),
                             'reason': {4: 'SL', 5: 'TP'}.get(getattr(d, 'reason', -1), 'بوت')} for d in deals]
            return st


# ---------- HTTP ----------
def make_handler(engine, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'x-token, content-type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(200, {})

        def _auth(self):
            if not secrets.compare_digest(self.headers.get('x-token', ''), token):
                self._send(401, {'error': 'الرمز غير صحيح'})
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if not u.path.startswith('/api/') or not self._auth():
                if not u.path.startswith('/api/'):
                    self._send(404, {'error': 'not found'})
                return
            try:
                if u.path == '/api/state':
                    return self._send(200, engine.state())
                if u.path == '/api/candles':
                    n = int(parse_qs(u.query).get('n', ['70'])[0])
                    return self._send(200, {'candles': engine.candles(n)})
                self._send(404, {'error': 'not found'})
            except BotError as e:
                self._send(400, {'error': str(e)})
            except Exception as e:
                self._send(500, {'error': repr(e)})

        def do_POST(self):
            u = urlparse(self.path)
            if not self._auth():
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n > 10000:
                    raise BotError('الطلب كبير بزاف')
                b = json.loads(self.rfile.read(n) or b'{}')
                if u.path == '/api/connect':
                    engine.connect(b.get('login'), b.get('password'), b.get('server'), b.get('symbol'))
                elif u.path == '/api/disconnect':
                    engine.disconnect()
                elif u.path == '/api/symbol':
                    engine.set_symbol(b.get('symbol'))
                elif u.path == '/api/bot':
                    engine.set_bot(b)
                elif u.path == '/api/order':
                    if b.get('side') not in ('buy', 'sell'):
                        raise BotError('اتجاه غير صالح')
                    engine.open_position(b['side'], b.get('lot'), 'يدوي', b.get('maxMin'))
                elif u.path == '/api/close':
                    engine.close_position()
                else:
                    return self._send(404, {'error': 'not found'})
                self._send(200, {'ok': True})
            except BotError as e:
                self._send(400, {'error': str(e)})
            except (ValueError, KeyError) as e:
                self._send(400, {'error': 'طلب غير صالح: %s' % e})
            except Exception as e:
                self._send(500, {'error': repr(e)})
    return H


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def load_token(path):
    t = os.environ.get('BRIDGE_TOKEN', '').strip()
    if t:
        return t
    try:
        t = open(path, encoding='utf8').read().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_hex(4)
    with open(path, 'w', encoding='utf8') as f:
        f.write(t)
    return t


def serve(engine, token, host, port):
    srv = ThreadingHTTPServer((host, port), make_handler(engine, token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def loop():
        while True:
            engine.tick()
            time.sleep(1.5)
    threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print('❌ مكتبة MetaTrader5 ناقصة. دير: pip install MetaTrader5')
        print('   (تخدم على ويندوز فقط. إذا pip ما لقاهاش، ثبّت Python 3.12)')
        sys.exit(1)
    port = int(os.environ.get('PORT', '8765'))
    token = load_token(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.txt'))
    engine = Engine(mt5)
    serve(engine, token, '0.0.0.0', port)
    ip = lan_ip()
    print('✅ الجسر شغال')
    print('   عنوان الجسر في التطبيق:  http://%s:%d' % (ip, port))
    print('   الرمز (Token):           %s' % token)
    print('   الحد الأقصى للحجم:       %s lot' % MAX_LOT)
    print('   خلي هذه النافذة مفتوحة، و MetaTrader 5 مفتوح، وزر Algo Trading مشعول.')
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        engine.disconnect()


if __name__ == '__main__':
    main()

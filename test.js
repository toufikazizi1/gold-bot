'use strict';
// يختبر منطق التطبيق (الجزء بين LOGIC_START و LOGIC_END): node test.js
const assert = require('assert');
const fs = require('fs');
const { execSync } = require('child_process');
const html = fs.readFileSync(__dirname + '/www/index.html', 'utf8');
const logic = html.split('//#LOGIC_START')[1].split('//#LOGIC_END')[0];
const L = new Function(logic + '\nreturn {hmac,roundStep,roundTick,stopAt,normCfg,createBot,core};')();
const reply = (data, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(data) });
const mem = () => { let v = null; return { get: () => v, set: x => { v = JSON.parse(JSON.stringify(x)); } }; };
const mk = arr => arr.map((c, i) => [60000 * i, c, c + 0.1, c - 0.1, c]);
const near = (a, b, e = 0.01) => assert(Math.abs(a - b) <= e, a + ' !~ ' + b);
const GOLD_INFO = { symbols: [{ symbol: 'XAUUSDT', filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.01' }, { filterType: 'MARKET_LOT_SIZE', stepSize: '0.001', minQty: '0.001' }, { filterType: 'MIN_NOTIONAL', notional: '5' }] }] };

(async () => {
  const qs = 'symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559', sec = 'NhqPtmdSSYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j';
  assert.strictEqual(await L.hmac(sec, qs), execSync(`printf %s '${qs}' | openssl dgst -sha256 -hmac '${sec}' | awk '{print $NF}'`).toString().trim());
  assert.strictEqual(L.roundStep(0.0075, 0.001), 0.007);
  assert.strictEqual(L.roundTick(3957.14285, 0.01), 3957.14);
  assert.strictEqual(L.stopAt('long', 4000, 0.007, -0.3, 0.01), 3957.14);
  assert.strictEqual(L.stopAt('short', 4000, 0.007, -0.3, 0.01), 4042.86);
  assert.strictEqual(L.stopAt('long', 4000, 0.007, 0.3, 0.01), 4042.86);
  assert.throws(() => L.normCfg({ mode: 'live', key: 'k', secret: 's' }), /LIVE/);
  assert.throws(() => L.normCfg({ mode: 'testnet' }), /Key/);
  assert.strictEqual(L.normCfg({ mode: 'live', key: 'k', secret: 's', confirm: 'LIVE', leverage: 50 }).leverage, 5);
  assert.deepStrictEqual(L.normCfg({}).symbols, ['XAUUSDT', 'PAXGUSDT']);

  const fall = Array.from({ length: 40 }, (_, i) => 4200 - i * 2);
  // ---------- paper ----------
  let price = 4000, candles = mk([...fall, 4120]);
  const fetchPaper = async url => {
    const u = new URL(url);
    if (u.pathname === '/fapi/v1/ticker/price') return reply({ price: String(price) });
    if (u.pathname === '/fapi/v1/klines') return reply(candles);
    if (u.pathname === '/fapi/v1/exchangeInfo') return reply(GOLD_INFO);
    throw new Error('unexpected ' + url);
  };
  const store = mem();
  const bot = L.createBot(L.normCfg({ mode: 'paper', maxMargin: 20 }), fetchPaper, store);
  assert.strictEqual(bot.S.bot.symbol, 'XAUUSDT');
  bot.setBot({ ind: 'RSI', margin: 10, sl: 0.3, tp: 0.6, lock: 0.2, on: true });
  await bot.tick();
  assert(bot.S.pos && bot.S.pos.side === 'long' && bot.S.pos.qty === 0.007, JSON.stringify(bot.S.pos));
  const q = 0.007;
  price = 4000 + 0.65 / q; await bot.tick();
  assert.strictEqual(bot.S.pos, null); assert.strictEqual(bot.S.trades[0].reason, 'TP');
  candles = mk([...fall, 4119, 4118]); price = 4000;
  await bot.tick(); assert(bot.S.pos);
  price = 4000 + 0.5 / q; await bot.tick(); assert(bot.S.pos); near(bot.S.pos.stopPnl, 0.3, 0.001);
  price = 4000 + 0.25 / q; await bot.tick();
  assert.strictEqual(bot.S.trades[0].reason, 'LOCK'); assert(bot.S.trades[0].pnl > 0.1 && bot.S.trades[0].pnl < 0.3);
  candles = mk([...fall, 4119, 4118, 4117]); price = 4000;
  await bot.tick(); price = 4000 - 0.35 / q; await bot.tick();
  assert.strictEqual(bot.S.trades[0].reason, 'SL');
  await assert.rejects(bot.openPos('XAUUSDT', 'long', 25, 'x'), /الحد الأقصى/);
  await assert.rejects(bot.openPos('BTCUSDT', 'long', 5, 'x'), /زوج/);
  const bot2 = L.createBot(L.normCfg({ mode: 'paper' }), fetchPaper, store);
  assert.strictEqual(bot2.S.trades.length, 3); assert.strictEqual(bot2.S.bot.on, false);
  bot.S.dayPnl = -5; assert.throws(() => bot.setBot({ on: true }), /الخسارة اليومي/);
  const hs = await bot2.getState(); assert(hs.hint && hs.hint.qty > 0);

  // ---------- زوج غير موجود ----------
  const noSym = L.createBot(L.normCfg({ mode: 'paper' }), async u => new URL(u).pathname.endsWith('exchangeInfo') ? reply({ symbols: [] }) : reply({ price: '4000' }), mem());
  await assert.rejects(noSym.openPos('XAUUSDT', 'long', 10, 'x'), /غير موجود/);

  // ---------- live ----------
  const calls = []; let posOpen = false, algoFail = false, closeFail = false, algoSeq = 100;
  price = 4000;
  const fetchLive = async (url, opt) => {
    const u = new URL(url); const c = { method: opt.method, path: u.pathname, q: Object.fromEntries(u.searchParams), key: opt.headers['X-MBX-APIKEY'] }; calls.push(c);
    if (u.pathname === '/fapi/v1/ticker/price') return reply({ price: String(price) });
    if (u.pathname === '/fapi/v1/exchangeInfo') return reply(GOLD_INFO);
    if (u.pathname === '/fapi/v2/positionRisk') return reply(posOpen ? [{ positionAmt: '0.007', entryPrice: '4000.00' }] : [{ positionAmt: '0', entryPrice: '0' }]);
    if (u.pathname === '/fapi/v1/marginType') return reply({ code: -4046, msg: 'No need to change margin type.' }, false, 400);
    if (u.pathname === '/fapi/v1/leverage') return reply({});
    if (u.pathname === '/fapi/v1/order') { const red = u.searchParams.get('reduceOnly'); if (red && closeFail) return reply({ code: -2022, msg: 'ReduceOnly Order is rejected.' }, false, 400); posOpen = !red; return reply({}); }
    if (u.pathname === '/fapi/v1/algoOrder') {
      if (opt.method === 'POST') { if (algoFail) return reply({ code: -4509, msg: 'TradFi agreement required' }, false, 400); return reply({ algoId: ++algoSeq }); }
      return reply({ algoId: +u.searchParams.get('algoId'), code: '200', msg: 'success' });
    }
    if (u.pathname === '/fapi/v2/balance') return reply([{ asset: 'USDT', availableBalance: '50.5' }]);
    throw new Error('unexpected ' + u.pathname);
  };
  const live = L.createBot(L.normCfg({ mode: 'live', key: 'KEY', secret: sec, confirm: 'LIVE', maxMargin: 10 }), fetchLive, mem());
  live.setBot({ margin: 10, sl: 0.3, tp: 0.6, lock: 0.2 });
  await live.openPos('XAUUSDT', 'long', 10, 'يدوي');
  const orders = calls.filter(c => c.path === '/fapi/v1/order');
  assert.deepStrictEqual([orders.length, orders[0].q.symbol, orders[0].q.side, orders[0].q.type, orders[0].q.quantity, orders[0].key], [1, 'XAUUSDT', 'BUY', 'MARKET', '0.007', 'KEY']);
  const { signature, ...rest } = orders[0].q; assert.strictEqual(signature, await L.hmac(sec, new URLSearchParams(rest).toString()));
  const st1 = calls.find(c => c.path === '/fapi/v1/algoOrder' && c.method === 'POST');
  assert.deepStrictEqual([st1.q.algoType, st1.q.symbol, st1.q.side, st1.q.type, st1.q.closePosition, st1.q.triggerPrice, st1.q.workingType], ['CONDITIONAL', 'XAUUSDT', 'SELL', 'STOP_MARKET', 'true', '3957.14', 'MARK_PRICE']);
  assert(calls.findIndex(c => c.path === '/fapi/v1/leverage') < calls.findIndex(c => c.path === '/fapi/v1/order'));
  assert(calls.findIndex(c => c.path === '/fapi/v1/order') < calls.findIndex(c => c.path === '/fapi/v1/algoOrder'));
  assert.strictEqual(live.S.pos.stopId, 101);
  // الوقف المتحرك: ربح 0.5$ -> وقف جديد عند +0.3$ ثم إلغاء القديم
  price = 4000 + 0.5 / 0.007; await live.tick();
  const posts = calls.filter(c => c.path === '/fapi/v1/algoOrder' && c.method === 'POST'), dels = calls.filter(c => c.method === 'DELETE');
  assert.strictEqual(posts.length, 2); assert.strictEqual(posts[1].q.triggerPrice, '4042.86');
  assert.strictEqual(dels.length, 1); assert.strictEqual(dels[0].q.algoId, '101');
  assert.strictEqual(live.S.pos.stopId, 102); near(live.S.pos.stopPnl, 0.3, 0.001);
  // تحسن صغير ما يحركش الوقف (تقليل الطلبات)
  price += 0.5; await live.tick(); assert.strictEqual(calls.filter(c => c.path === '/fapi/v1/algoOrder' && c.method === 'POST').length, 2);
  // باينانس نفذ الوقف
  posOpen = false; live.S.lastVerify = 0; await live.tick();
  assert.strictEqual(live.S.pos, null); assert.strictEqual(live.S.trades[0].reason, 'LOCK-BN'); near(live.S.trades[0].pnl, 0.3, 0.01);
  // إغلاق يدوي يلغي وقف باينانس
  const nDel = calls.filter(c => c.method === 'DELETE').length; price = 4000;
  await live.openPos('XAUUSDT', 'long', 10, 'يدوي'); await live.closePos('يدوي');
  assert.strictEqual(calls.filter(c => c.method === 'DELETE').length, nDel + 1); assert.strictEqual(live.S.pos, null);
  // أمان: فشل الوقف => إغلاق فوري
  algoFail = true; const nOrd = calls.filter(c => c.path === '/fapi/v1/order').length;
  await assert.rejects(live.openPos('XAUUSDT', 'long', 10, 'x'), /سكّرت الصفقة/);
  const ords = calls.filter(c => c.path === '/fapi/v1/order'); assert.strictEqual(ords.length, nOrd + 2); assert.strictEqual(ords[ords.length - 1].q.reduceOnly, 'true'); assert.strictEqual(live.S.pos, null);
  // فشل الوقف + فشل الإغلاق => تحذير والصفقة تبقى ظاهرة
  closeFail = true; await assert.rejects(live.openPos('XAUUSDT', 'long', 10, 'x'), /بلا وقف/); assert(live.S.pos);
  console.log('ALL_TESTS_PASS');
})().catch(e => { console.error(e); process.exit(1); });

//+------------------------------------------------------------------+
//| GoldScalper.mq5 - بوت لحظي يشتغل داخل MT5 مباشرة (بدون جسر)      |
//| يفتح ويغلق صفقات صغيرة بسرعة على الذهب، مع وقف متحرك وحدود أمان   |
//| جرّبه على الحساب التجريبي فقط.                                   |
//+------------------------------------------------------------------+
#property copyright "Gold Scalper"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input group "=== الصفقات ==="
input double InpLot              = 0.01;   // حجم الصفقة (Lot)
input int    InpMaxPositions     = 3;      // أقصى عدد صفقات مفتوحة معاً
input int    InpMaxOrdersPerMin  = 100;    // أقصى عدد صفقات في الدقيقة
input int    InpMaxHoldSec       = 8;      // إغلاق إجباري بعد كم ثانية (0 = بدون)
input int    InpMaxSpreadPts     = 600;    // أقصى سبريد مسموح (نقاط)

input group "=== الإشارة (حركة لحظية) ==="
input int    InpMomentumTicks    = 6;      // عدد التكات الأخيرة للمقارنة
input double InpMinMovePrice     = 0.08;   // أقل حركة بالسعر لفتح صفقة (مثلا 0.08$)

input group "=== الوقف والربح بالدولار ==="
input double InpSL_USD           = 1.0;    // وقف الخسارة لكل صفقة ($)
input double InpTP_USD           = 0.0;    // هدف الربح ($) - 0 = بدون
input double InpTrailStart_USD   = 0.25;   // يبدأ الوقف المتحرك لما الربح يوصل ($)
input double InpTrailGap_USD     = 0.15;   // الوقف يبقى وراء أفضل ربح بهذا المبلغ ($)

input group "=== حدود الأمان اليومية ==="
input double InpDailyLoss_USD    = 10.0;   // يتوقف إذا خسر اليوم ($)
input double InpDailyProfit_USD  = 0.0;    // يتوقف إذا ربح اليوم ($) - 0 = بدون

input group "=== عام ==="
input long   InpMagic            = 777002; // رقم يميز صفقات هذا البوت

CTrade   trade;
double   g_ticks[];          // آخر أسعار (وسط السعر)
int      g_tickCount = 0;
ulong    g_tickets[];        // أفضل ربح لكل صفقة
double   g_best[];
ulong    g_lastOpenMs = 0;
ulong    g_openTimes[];      // أوقات فتح الصفقات في آخر دقيقة (ms)
bool     g_stopped = false;
datetime g_stopDay = 0;
double   g_dayPnl = 0.0;
ulong    g_lastPnlMs = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(50);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);
   ArrayResize(g_ticks, MathMax(InpMomentumTicks, 2));
   EventSetMillisecondTimer(100);
   Print("GoldScalper جاهز على ", _Symbol, " | Lot=", InpLot, " | Max=", InpMaxPositions);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
//| أمور مساعدة                                                       |
//+------------------------------------------------------------------+
double ContractSize()
  {
   double cs = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   return (cs > 0 ? cs : 100.0);
  }

int CountMyPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic) n++;
     }
   return n;
  }

double BestFor(ulong ticket, double profit)
  {
   int n = ArraySize(g_tickets);
   for(int i = 0; i < n; i++)
      if(g_tickets[i] == ticket)
        {
         if(profit > g_best[i]) g_best[i] = profit;
         return g_best[i];
        }
   ArrayResize(g_tickets, n + 1);
   ArrayResize(g_best, n + 1);
   g_tickets[n] = ticket;
   g_best[n] = MathMax(profit, 0.0);
   return g_best[n];
  }

void CleanBest()
  {
   // نحذف الصفقات المغلقة من الذاكرة
   for(int i = ArraySize(g_tickets) - 1; i >= 0; i--)
      if(!PositionSelectByTicket(g_tickets[i]))
        {
         int last = ArraySize(g_tickets) - 1;
         g_tickets[i] = g_tickets[last];
         g_best[i] = g_best[last];
         ArrayResize(g_tickets, last);
         ArrayResize(g_best, last);
        }
  }

double DayClosedPnl()
  {
   datetime now = TimeCurrent();
   datetime start = now - (now % 86400);
   if(!HistorySelect(start, now + 86400)) return g_dayPnl;
   double sum = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;
      if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      sum += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_COMMISSION)
           + HistoryDealGetDouble(d, DEAL_SWAP) + HistoryDealGetDouble(d, DEAL_FEE);
     }
   return sum;
  }

double OpenPnl()
  {
   double s = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         s += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return s;
  }

void CheckDayLimits()
  {
   datetime now = TimeCurrent();
   datetime day = now - (now % 86400);
   if(day != g_stopDay) { g_stopDay = day; g_stopped = false; }   // يوم جديد
   ulong ms = GetTickCount64();
   if(ms - g_lastPnlMs < 1000) return;
   g_lastPnlMs = ms;
   g_dayPnl = DayClosedPnl() + OpenPnl();
   if(!g_stopped && InpDailyLoss_USD > 0 && g_dayPnl <= -InpDailyLoss_USD)
     { g_stopped = true; Print("تم الإيقاف: بلغ حد الخسارة اليومي ", DoubleToString(g_dayPnl, 2)); }
   if(!g_stopped && InpDailyProfit_USD > 0 && g_dayPnl >= InpDailyProfit_USD)
     { g_stopped = true; Print("تم الإيقاف: بلغ حد الربح اليومي ", DoubleToString(g_dayPnl, 2)); }
  }

//+------------------------------------------------------------------+
//| إدارة الصفقات المفتوحة: وقف متحرك + إغلاق بالوقت                  |
//+------------------------------------------------------------------+
void ManagePositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;

      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      double best = BestFor(t, profit);
      bool close = false;

      if(InpSL_USD > 0 && profit <= -InpSL_USD) close = true;
      if(InpTP_USD > 0 && profit >= InpTP_USD) close = true;
      if(InpTrailStart_USD > 0 && best >= InpTrailStart_USD && profit <= best - InpTrailGap_USD) close = true;
      if(InpMaxHoldSec > 0)
        {
         long held = (long)(TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME));
         if(held >= InpMaxHoldSec) close = true;
        }
      if(close) trade.PositionClose(t);
     }
   CleanBest();
  }

//+------------------------------------------------------------------+
//| فتح صفقة                                                          |
//+------------------------------------------------------------------+
bool RateLimitOk()
  {
   ulong now = GetTickCount64();
   // نحذف أوقات أقدم من دقيقة
   int n = ArraySize(g_openTimes), k = 0;
   for(int i = 0; i < n; i++)
      if(now - g_openTimes[i] < 60000) g_openTimes[k++] = g_openTimes[i];
   ArrayResize(g_openTimes, k);
   return (k < InpMaxOrdersPerMin);
  }

void TryOpen(bool buy)
  {
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double per = InpLot * ContractSize();            // دولار لكل 1.00 حركة سعر
   double slDist = (InpSL_USD > 0 && per > 0) ? InpSL_USD / per : 0.0;
   // وقف خسارة على السيرفر كحماية إضافية (أبعد قليلا من وقف البوت)
   double sl = 0.0;
   if(slDist > 0)
      sl = NormalizeDouble(buy ? ask - slDist * 1.5 : bid + slDist * 1.5, digits);

   bool ok = buy ? trade.Buy(InpLot, _Symbol, ask, sl, 0.0, "GS")
                 : trade.Sell(InpLot, _Symbol, bid, sl, 0.0, "GS");
   if(ok && (trade.ResultRetcode() == TRADE_RETCODE_DONE || trade.ResultRetcode() == TRADE_RETCODE_PLACED))
     {
      int n = ArraySize(g_openTimes);
      ArrayResize(g_openTimes, n + 1);
      g_openTimes[n] = GetTickCount64();
      g_lastOpenMs = GetTickCount64();
     }
   else
      Print("فشل فتح الصفقة: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| الإشارة اللحظية: مقارنة السعر الحالي بمتوسط آخر التكات            |
//+------------------------------------------------------------------+
void OnTick()
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(bid <= 0 || ask <= 0) return;
   double mid = (bid + ask) / 2.0;

   int need = MathMax(InpMomentumTicks, 2);
   if(ArraySize(g_ticks) != need) ArrayResize(g_ticks, need);
   // نحفظ التكة في آخر القائمة
   for(int i = 0; i < need - 1; i++) g_ticks[i] = g_ticks[i + 1];
   g_ticks[need - 1] = mid;
   if(g_tickCount < need) { g_tickCount++; ManagePositions(); return; }

   ManagePositions();
   CheckDayLimits();
   if(g_stopped) return;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED)) return;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int spread = (int)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(InpMaxSpreadPts > 0 && spread > InpMaxSpreadPts) return;
   if(CountMyPositions() >= InpMaxPositions) return;
   if(!RateLimitOk()) return;
   // فاصل أدنى بين صفقتين حسب الحد في الدقيقة
   ulong minGap = (InpMaxOrdersPerMin > 0) ? (ulong)(60000 / InpMaxOrdersPerMin) : 0;
   if(GetTickCount64() - g_lastOpenMs < minGap) return;

   double sum = 0.0;
   for(int i = 0; i < need - 1; i++) sum += g_ticks[i];
   double avg = sum / (need - 1);
   double move = mid - avg;

   if(move >= InpMinMovePrice)       TryOpen(true);
   else if(move <= -InpMinMovePrice) TryOpen(false);
  }

//+------------------------------------------------------------------+
//| مؤقت 100ms: يحدّث الوقف المتحرك حتى بدون تكات جديدة               |
//+------------------------------------------------------------------+
void OnTimer()
  {
   ManagePositions();
   CheckDayLimits();
  }
//+------------------------------------------------------------------+

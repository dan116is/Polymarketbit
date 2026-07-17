# POLYSIGNAL

מערכת איתותים אישית לשוקי ה-BTC Up/Down של 5 דקות ב-Polymarket: מנוע fair-value
דטרמיניסטי + אפליקציית PWA לטלפון + התראות טלגרם. **שום AI לא מנבא כיוון,
והקוד לא מבצע שום מסחר בכסף אמיתי** — המערכת מחשבת את הפער בין ההסתברות
המתמטית לעלייה לבין המחיר המצוטט בספר הפקודות, ואומרת `UP / DOWN / PASS`
עם המלצת סכום ‎$1/$2/$5, מאחורי שני שערי GO/NO-GO כמותיים (backtest ואז paper).

נבנה לפי `POLYSIGNAL-MASTER-PLAN.md` (מסמך ה-INIT של הפרויקט). גבולות קשיחים:
איתותים בלבד, מקסימום ‎$5 להמלצה, הפסד יומי במעקב של ‎−$10 ← נעילת LIVE
ל-24 שעות, אין מרטינגייל, ותצוגת LIVE מסרבת להיפתח כל עוד GATE 1 + GATE 2
לא ירוקים במסד הנתונים — אין override בקוד.

## ארכיטקטורה

```
WATCHER                     QUANT                   RISK              DELIVERY
Binance WS (פרוקסי ספוט)    P_up = Φ(ln(S/S₀)/σ√τ)  סף EV‏ θ           התראות טלגרם + /kill
RTDS Chainlink WS (אורקל)   σ = EWMA תשואות שנייה   סולם $1/$2/$5     PWA (כרטיס חי ב-WS)
CLOB REST (ספרים)           EV מול ask − עמלה − buf  stop יומי, נעילות
Gamma (‏slug ← מטא-דאטה)               ↓
                            SQLite: כל tick, איתות ותוצאה ← דוח ANALYST יומי
```

- **זיהוי השוק דטרמיניסטי**: `window_ts = now − (now % 300)` ←
  slug‏ `btc-updown-5m-{window_ts}` ← Gamma מחזיר `clobTokenIds`, עמלה ו-tick.
  בלי חיפוש, בלי תלות באינדוקס. (אומת חי: זיהוי ≤0.2 שניות.)
- **מחיר הפתיחה מגיע ממקור ה-resolution** (Chainlink BTC/USD), דרך ה-RTDS
  WebSocket של Polymarket (טופיק `crypto_prices_chainlink`, סימבול `btc/usd`,
  רזולוציית שנייה, ‏backlog של ~70–120 שניות בכל subscribe, בלי push שוטף ←
  הלקוח עושה resubscribe כל 10 שניות). Binance הוא רק פרוקסי מהיר: המנוע
  מתעגן על נקודת האורקל האחרונה ומוסיף את תזוזת Binance מאז, כך שהבסיס
  של ~$45 בין Binance לאורקל מתבטל מעצם הבנייה.
- **העמלות נקראות מה-API בזמן ריצה** (`takerBaseFee`, כרגע 1000bps =
  ‏10% × min(p, 1−p) למניה) — לעולם לא מקובעות בקוד.

## מבנה הפרויקט

| נתיב | מה זה |
|---|---|
| `polysignal/quant.py` | ‏fair value, ‏EWMA, ‏EV, סולם הימור (פונקציות טהורות) |
| `polysignal/feeds.py` | ‏Binance WS, אורקל RTDS, ‏poller לספרים (התאוששות אוטומטית) |
| `polysignal/engine.py` | מכונת מצבים לכל חלון (M1) |
| `polysignal/risk.py` | חוקי כסף: שערים, stop יומי, cooldown, מתג חירום |
| `polysignal/store.py` | סכמת SQLite לפי סעיף 4.4 + דגלי שערים |
| `polysignal/delivery/` | טלגרם (התראות + פקודות) ושרת ה-PWA |
| `polysignal/pwa/` | אפליקציית הטלפון (מוגשת מהמנוע) |
| `web/` + `netlify.toml` | מוניטור עצמאי לדפדפן — נפרס ל-Netlify, בלי שרת |
| `scripts/m0_discovery.py` | ‏spike של M0: הוכחת זיהוי + ספר + פתיחת אורקל, חי |
| `scripts/backtest.py` | ‏harness של M2: שחזור 15.7K חלונות, סריקת רשת, GATE 1 |
| `scripts/gate2_check.py` | הערכת GATE 2 על לוג ה-PAPER |
| `scripts/analyst_report.py` | דוח כיול יומי (כל מספר עם שאילתת SQL) |
| `reports/` | ראיות: לוג M0, דוח backtest, דוח NO-GO, צילומי מסך |

## התחלה מהירה

```bash
pip install -r requirements.txt
cp .env.example .env            # למלא TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

python scripts/m0_discovery.py 30          # ‏spike של M0 (ראיות)
python scripts/run_engine.py               # מנוע במצב PAPER‏ (M1/M4)
python scripts/run_engine.py --with-delivery   # + טלגרם + PWA על :8787
python -m pytest tests/                    # כולל טסט נעילת ה-24 שעות

# ‏Backtest‏ (M2): הורדת דאטה פעם אחת, ואז הסריקה
python scripts/fetch_binance_1s.py 2026-03-24 2026-05-18
python scripts/backtest.py                 # כותב reports/backtest.md + דגל GATE1
python scripts/gate2_check.py              # אחרי ≥200 איתותי paper / 7 ימים
```

בטלפון: לפתוח `http://<שרת>:8787` ולהוסיף למסך הבית. הכרטיס מציג
UP/DOWN/PASS, ספירה לאחור, P_fair מול השוק, פער והמלצת סכום; אם השרת מפסיק
לשדר — המסך עובר למצב OFFLINE מפורש (לעולם לא מסך קפוא שנראה חי).

שירות Windows: להריץ `scripts/run_polysignal.bat` דרך Task Scheduler או NSSM
עם הפעלה-מחדש אוטומטית; המצב שורד ריסטארטים (SQLite, כתיבה אידמפוטנטית לכל חלון).

## מצב מול התוכנית (בכנות)

- **M0 ✓** — זיהוי דטרמיניסטי אומת חי; פתיחת האורקל נתפסת מפיד ה-Chainlink
  של RTDS; עמלה/tick/minSize נקראים מה-API; הבסיס נמדד.
  ראיות: `reports/m0_discovery.log`.
- **M1 ✓** — המנוע רץ, רושם כל חלון ו-tick ל-SQLite, מתאושש מניתוקי WS,
  כתיבה אידמפוטנטית. (ריצת יציבות של 24 שעות ממתינה למחשב הקבוע.)
- **M2 ✓ / GATE 1: אדום ← NO-GO ל-LIVE ידני** — המודל מכויל (Brier‏ 0.19)
  ויש אדג' אמיתי ב-latency אפס (‎+3.8¢ למניה אחרי עמלות), אבל הוא דועך
  ‎~1¢ לכל שנייה של עיכוב ביצוע: כל 300 הקונפיגורציות ו-3 איטרציות המודל
  שליליות עם 5 שניות של יד אנושית. פסק הדין המלא: `reports/GATE1_NO_GO.md`.
  הנתיב הריאלי היחיד לאדג' הוא M6 (בוט) — החלטה נפרדת ומפורשת.
- **M3 ✓ קוד** — התראות טלגרם + פקודות מתג חירום, PWA. אימות בטלפון עצמו
  (צילום מסך, ‏p95 ≤ 2 שניות) דורש את המכשיר של דניאל + טוקן.
- **M4 מוכן** — מצב PAPER עם ‏latency יד מדומה של 5 שניות הוא ברירת המחדל;
  להריץ 5–7 ימים ואז `gate2_check.py`.
- **M5 נעול** — תצוגת LIVE מסרבת אוטומטית כל עוד השערים אדומים (מכוסה בטסט).
- **M6 (בוט ביצוע אוטומטי) מחוץ לתחולה** — החלטה מפורשת נפרדת.

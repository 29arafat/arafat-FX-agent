# Arafat FX Agent

Python ও GitHub Actions দিয়ে তৈরি ফ্রি ফরেক্স ও গোল্ড সিগন্যাল সিস্টেম। এটি শুধু সিগন্যাল দেয়, নিজে ট্রেড করে না।

> **সতর্কতা:** এই রিপো **পাবলিক**। সিগন্যাল ও ফলাফল সবাই দেখতে পাবে। কোনো Token বা API Key কখনো কোডে, config-এ বা চ্যাটে লিখবেন না। শুধু GitHub Secrets-এ।

## কী করে
- পেয়ার: XAUUSD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD
- টাইমফ্রেম: H1, H4, D1 (প্রতিটির স্বতন্ত্র সিগন্যাল)
- প্রতি ঘণ্টায় GitHub Actions চলে, নতুন সিগন্যাল Telegram-এ যায়, ফলাফল `data` ব্রাঞ্চে JSON-এ জমা হয়
- ফোনে ইনস্টলযোগ্য PWA অ্যাপ ফলাফল দেখায় (ধাপ ৬-৭)

## কাঠামো
```
config.yaml            সব সংখ্যা ও প্যারামিটার (এখানেই বদলান)
requirements.txt       Python প্যাকেজ
src/                   ইঞ্জিন (fetch ✅, indicators ✅; বাকিগুলো পরের ধাপে)
backtest/              ব্যাকটেস্ট
tests/                 স্বয়ংক্রিয় টেস্ট
web/                   PWA অ্যাপ
scripts/apply_zip.sh   zip থেকে ফাইল বসিয়ে কমিট করার সহজ স্ক্রিপ্ট
.github/workflows/     signals.yml (ঘণ্টায়), backtest.yml (হাতে চালানো)
PROJECT_STATE.md       প্রজেক্টের বর্তমান অবস্থা
```
শাখা: `main` = কোড ও অ্যাপ। `data` = শুধু JSON (`signals.json`, `stats.json`, `status.json`), ধাপ ৫-এ নিজে তৈরি হবে।

## GitHub Secrets (শুধু নাম, মান নয়)
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `PRICE_API_KEY` (Twelve Data)

## API ব্যবহার (Twelve Data ফ্রি: ৮ রিকোয়েস্ট/মিনিট, ৮০০/দিন)
প্রতি রিকোয়েস্ট ১ ক্রেডিট, একবারে একটা পেয়ার। **শুধু H1 আনা হয়**, H4 ও D1 H1 থেকে বানানো।

| কাজ | হিসাব | রিকোয়েস্ট/দিন |
|---|---|---|
| H1 (প্রতি ঘণ্টা, ৩৫০০ ক্যান্ডেল) | ৬ পেয়ার × ২৪ | ১৪৪ |
| D1-এর ঘণ্টায় বাড়তি দ্বিতীয় পেজ (৯০০০ H1) | ৬ পেয়ার × ১ | ৬ |
| H4, D1 | আলাদা কল নয় | ০ |
| **মোট** | | **~১৫০ / ৮০০** |

মিনিটের সীমা মানতে রিকোয়েস্টের মাঝে ~৮ সেকেন্ড বিরতি থাকে (প্রতি রান ~১ মিনিট)।

## গুরুত্বপূর্ণ ডেটা নিয়ম
- সময় সবসময় `timezone=UTC` পাঠানো হয় (প্রোভাইডারের ডিফল্ট UTC নয়)।
- সর্বশেষ ক্যান্ডেল চলমান, তাই বাদ দেওয়া হয় (সিগন্যাল পরে বদলায় না)।
- প্রোভাইডারের নিজস্ব H4/D1 গ্রিড স্থির নয় (আসল ডেটায় প্রমাণিত), তাই **H4 (০,৪,৮… UTC) ও D1 (`d1_day_start_hour`, এখন ২০:০০ UTC) H1 থেকে নিজেরা বানানো হয়**।
- আসল ডেটায় দেখা গেছে প্রোভাইডার সপ্তাহান্তেও H1 ক্যান্ডেল দেয় (গোল্ডে প্রায় ২৪/৭, ফাঁকা নয়, সামান্য নড়াচড়াসহ)। তাই **শুক্র ২২:০০ থেকে রবি ২২:০০ UTC-র ক্যান্ডেল বাদ** (`drop_weekend_candles`, জানালা `weekend`), আর শূন্য-রেঞ্জ ক্যান্ডেলও বাদ (`drop_flat_candles`)। নইলে EMA/ATR কমে যেত আর দিনের গণনা ভেঙে যেত। `--diagnose` দিয়ে যাচাই করা যায়। (শীতে/গ্রীষ্মে বাজারের সময় ১ ঘণ্টা সরে, তাই জানালার কিনারায় ১ ঘণ্টার ক্যান্ডেল বাদ পড়তে পারে, ক্ষতি নেই।)
- আপনার চার্টের D1 ~২১:০০ UTC-তে (নিউ ইয়র্ক ৫টা) শুরু। ১ ঘণ্টার ফারাক ও ১-২ ডলারের দামের ফারাক গ্রহণযোগ্য ধরা হয়েছে।

## নতুন ফাইল বসানোর সহজ নিয়ম (ফোনে, Codespaces টার্মিনালে)
1. Claude-এর দেওয়া zip GitHub রিপোতে আপলোড করুন (Add file → Upload files → Commit)।
2. Codespaces খুলে টার্মিনালে লিখুন:
   `bash scripts/apply_zip.sh <zip-ফাইলের-নাম>.zip "<কমিট মেসেজ>"`

## ডেটা যাচাই (এক কমান্ডে)
**Actions-এ (দাম ছাড়া):** Actions → **data-check** → Run workflow। সব পেয়ারের ক্যান্ডেল সংখ্যা, সময় ও ক্রেডিট ব্যবহার দেখাবে। লগ পাবলিক, তাই দাম ছাপে না।

**Codespaces-এ (দামসহ, শুধু আপনি দেখবেন):**
1. টার্মিনালে `read -s PRICE_API_KEY` লিখে Enter চাপুন, Key পেস্ট করে আবার Enter (কিছু দেখাবে না, ইতিহাসেও থাকবে না)।
2. `export PRICE_API_KEY`
3. `pip install -r requirements.txt` (একবার)
4. `python -m src.fetch --check`
   শুধু গোল্ড: `python -m src.fetch --check --pairs XAUUSD`
   রোগনির্ণয় (প্রোভাইডারের নিজস্ব H4 কোন ঘণ্টায় শুরু হয়): `python -m src.fetch --check --pairs XAUUSD --no-prices --diagnose`

আউটপুটে প্রতি পেয়ারে H1, H4 (শেষ ২টা) ও D1 (শেষ ৩টা) ক্যান্ডেল আসে।

## ধাপের অবস্থা
০ ✅ সেটআপ · ১ ✅ কাঠামো · ২ ✅ ডেটা ও ইন্ডিকেটর · ৩ ✅ ব্যাকটেস্ট · ৪ ✅ সিগন্যাল ইঞ্জিন ও Telegram · ৫ ✅ ট্র্যাকিং, stats, data ব্রাঞ্চ, স্বয়ংক্রিয় রান · ৬ অ্যাপ (কোড তৈরি) · ৭ ডিপ্লয় · ৮ লাইভ টেস্ট

## ব্যাকটেস্ট (ধাপ ৩)

```
python -m backtest.backtest --years 3            # সব পেয়ার
python -m backtest.backtest --pairs XAUUSD --years 3
python -m backtest.backtest --refresh            # ক্যাশ ফেলে নতুন ডেটা
```
- Windows PowerShell-এ Key: `$env:PRICE_API_KEY='আপনার-key'` (একই জানালায়, এক লাইনে)। Codespaces/bash-এ: `export PRICE_API_KEY='…'`
- ফল: `backtest/output/report.md` (বাংলা রিপোর্ট) ও `summary.json`। ইতিহাস `backtest/cache/`-এ জমে, পরের বার API লাগে না।
- GitHub-এ চালাতে: Actions → backtest → Run workflow (রিপোর্টে কোনো কাঁচা দাম নেই)।
- স্প্রেড `config.yaml → backtest.spread`-এ। এগুলো প্রাথমিক অনুমান; Exness-এর আসল স্প্রেড দিলে বসানো হবে।

## সিগন্যাল ইঞ্জিন (ধাপ ৪)

কৌশল: `strategy.version` (v1-experimental), শুধু XAUUSD। রান:
```
python -m src.main --test-telegram        # Telegram সংযোগ পরীক্ষা (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID লাগে)
python -m src.main --once                 # ড্রাই-রান: বার্তা শুধু টার্মিনালে
python -m src.main --once --send          # আসল Telegram-এ
```
- Windows PowerShell: `$env:PRICE_API_KEY='…'`, `$env:TELEGRAM_TOKEN='…'`, `$env:TELEGRAM_CHAT_ID='…'` (একই জানালায়; এগুলো কোথাও কমিট করবেন না)।
- অবস্থা `state/signals.json`-এ (লোকাল, gitignored)। একই ক্যান্ডেলের সিগন্যাল দুবার যায় না।

## স্বয়ংক্রিয় রান ও data ব্রাঞ্চ (ধাপ ৫)

- `signals.yml` প্রতি ঘণ্টার ৭ মিনিটে চলে: ডেটা → সিগন্যাল → Telegram → `data` ব্রাঞ্চে কমিট। `tests.yml` শুধু পরীক্ষা চালায়।
- `data` ব্রাঞ্চের ফাইল: `signals.json`, `status.json`, `stats.json`, `state.json` (সব পাবলিক, কোনো Secret নেই, বর্তমান দাম নেই)।
- Actions চালু থাকলে নিজের পিসি থেকে `--send` চালাবেন না (দুই জায়গার state আলাদা, ডুপ্লিকেট বার্তা)। পিসিতে ড্রাই-রান (`--once`) নিরাপদ।
- অচলতা ধরার উপায়: (১) প্রতিদিন "💚 সিস্টেম সচল" বার্তা (না এলে cron বন্ধ), (২) workflow ব্যর্থ হলে সতর্কবার্তা, (৩) বিরতি > ৩ ঘণ্টা হলে পরের রানে সতর্কবার্তা।
- GitHub পাবলিক রিপোতে ৬০ দিন "কার্যকলাপ" না থাকলে schedule নিজে বন্ধ করে দেয় (কার্যকলাপের সংজ্ঞা নথিতে নেই)। তাই মাসে অন্তত একবার রিপোতে নিজে কমিট করুন, আর দৈনিক সচল-বার্তা বন্ধ হলে Actions ট্যাবে "Enable workflow" চাপুন।

## অ্যাপ (PWA, ধাপ ৬)

`web/` ফোল্ডারে: `index.html`, `style.css`, `app.js`, `config.js`, `sw.js`, `manifest.webmanifest`, `icons/`। কোনো বিল্ড লাগে না।
- অ্যাপ `data` ব্রাঞ্চের `signals.json` ও `status.json` পড়ে (`config.js → dataBase`), প্রতি ৫ মিনিটে রিফ্রেশ, শেষ ডেটা ফোনে সংরক্ষিত থাকে (অফলাইনে দেখায়)।
- পিসিতে চালিয়ে দেখতে: `python -m http.server 8000 -d web` তারপর ব্রাউজারে `http://localhost:8000`।
- ফাইল বদলালে `sw.js`-এর `VERSION` বাড়ান, নইলে ফোনে পুরনো সংস্করণ দেখাতে পারে।
- সময় সৌদি (UTC+3)। বর্তমান দাম পাবলিক JSON-এ নেই, তাই চলমান কার্ডে SL/এন্ট্রি/TP-র মই দেখায়।
- ডিপ্লয় (Cloudflare Pages) ধাপ ৭-এ।


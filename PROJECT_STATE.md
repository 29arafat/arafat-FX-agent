## PROJECT_STATE (আপডেট: ২০২৬-১০-২৩)
- সম্পন্ন ধাপ: ০, ১, ২ (কোড ও টেস্ট তৈরি; আসল ডেটা-যাচাই আপনার হাতে)
- বর্তমান ধাপ: ২ (fetch, indicators), অবস্থা: সংশোধিত zip প্রস্তুত (step2b), আসল --check আবার চালানো বাকি
- রিপোজিটরি: 29arafat/arafat-FX-agent (পাবলিক), শাখা: main (data ধাপ ৫-এ)
- ডেলিভারি: zip আপলোড + scripts/apply_zip.sh (সরাসরি কমিট সম্ভব হয়নি)
- Secrets আছে: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, PRICE_API_KEY (Twelve Data)
- বট: @arafatFXagentbot, ব্যক্তিগত চ্যাট (গ্রুপে পরে Secret বদলে)
- যাচাই করা তথ্য (২০২৬-১০-২৩): Twelve Data ফ্রি ৮/মিনিট, ৮০০/দিন;
  XAU/USD H1, H4 ফ্রি Key-তে চলে; ৫০০০ ক্যান্ডেল/কল (H4 ~২.৭ বছর)
- ডেটা নিয়ম: timezone=UTC সবসময়; সর্বশেষ ক্যান্ডেল চলমান, বাদ; datetime = শুরু
- H4/D1: প্রোভাইডারের নিজস্ব গ্রিড অস্থির (আসল ডেটায় H4 সারিবদ্ধ নয়)। শুধু H1 আনি;
  H4=০,৪,৮… UTC, D1=d1_day_start_hour (২০) H1 থেকে বানাই। ব্যবহারকারীর TradingView D1 ~২১:০০
  UTC (NY ৫টা); ১ ঘণ্টা/১-২ ডলার ফারাক গ্রহণযোগ্য। D1-এর জন্য ৭০০০ H1 (২ পেজ), ক্রেডিট ~১৫০/দিন
- সিদ্ধান্ত: ৬ পেয়ার; H1/H4/D1 স্বতন্ত্র; Running পেয়ার+টাইমফ্রেম প্রতি ১;
  ট্র্যাকিং H1-এ; ইন্ডিকেটর নিজে লেখা; provider adapter; heartbeat; notified
  ফ্ল্যাগ; ব্যাকটেস্টে স্প্রেড ও out-of-sample; cron "7 * * * *"; গোল্ড ২ দশমিক
- বাকি যাচাই: ফরেক্স পেয়ার ফ্রি Key-তে, Twelve Data "non-display" শর্ত,
  ইতিহাস কত পেছনে, ৬০ দিনের Actions নিয়ম, Cloudflare Pages বনাম Workers
- জানা সমস্যা / বাকি কাজ: রিপো সুরক্ষা সেটিংস, বটে Start, Codespaces প্রথম চালু
- ফাইল পরিবর্তন (শেষ zip): step2d-weekend-window.zip (fetch.py, config.yaml, tests, README, apply_zip.sh)
- ধাপ ২ নিয়ম: Key হেডারে (URL-এ নয়); D1 H4 থেকে বানানো, সপ্তাহান্তের টুকরো জোড়া; min_bars ২৫০; Actions v6, ubuntu-24.04
- ডেটা-শর্ত: ফ্রি ডেটা বাণিজ্যিক নয়, বাইরে প্রদর্শন/পুনর্বিতরণ অনুমোদন ছাড়া নয় → পাবলিক রিপোতে কাঁচা দাম/ক্যান্ডেল কমিট করব না; last_price পাবলিক JSON-এ রাখা নিয়ে ধাপ ৫-৬-এ সিদ্ধান্ত
- আসল --check (২০২৬-১০-২৩): ৬ পেয়ারই সফল (ফরেক্সসহ), ক্রেডিট ১২। প্রমাণ: প্রোভাইডার সপ্তাহান্তেও ফাঁকা
  নয় এমন H1 দেয় (গোল্ড ~২৩/দিন, D1 ~৩৮০ > ২৯৬ কর্মদিবস; drop_flat প্রায় কিছুই বাদ দেয়নি)।
  সমাধান: drop_weekend_candles (শুক্র ২২:০০–রবি ২২:০০ UTC বাদ) + drop_flat_candles;
  h1_bars ৩৫০০, d1_h1_bars ৯০০০। বাকি: নতুন --check-এ D1 ~২৬০-২৯০ আসে কিনা দেখা
- জানা: XAUUSD H1 আসল Key-তে কাজ করে; ফরেক্স পেয়ার ও নতুন H1-ভিত্তিক --check আসল ডেটায় যাচাই বাকি
- Codespaces-এ Key: export PRICE_API_KEY='…' (এক লাইনে); স্থায়ী: Codespaces Secret
- ধাপ ৩ কোড তৈরি (step3-backtest.zip): strategy.py (Frame/evaluate, tf_map H1:D1→H4→H1, H4:D1→H4→H4, D1 নিজে),
  tracker.py, stats.py, backtest/backtest.py, fetch_h1_history, clean_h1; স্কোর required [ট্রেন্ড, পুলব্যাক, কনফার্মেশন] + threshold ৭
  (প্রাথমিক, টিউন করা নয়); দুই পাস gross/net; IS ৭০%/OOS ৩০%; look-ahead replay টেস্ট; ৫৯ টেস্ট স্যান্ডবক্সে পাস
- পরের পদক্ষেপ: apply zip → `python -m backtest.backtest --years 3` চালিয়ে report.md ফল জানানো; স্প্রেড ও ফল দেখে ধাপ ৩ শেষ, তারপর ধাপ ৪

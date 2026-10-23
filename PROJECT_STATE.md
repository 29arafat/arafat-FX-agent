## PROJECT_STATE (আপডেট: ২০২৬-১০-২৩)
- সম্পন্ন ধাপ: ০, ১
- বর্তমান ধাপ: ১ (রিপো কাঠামো), অবস্থা: শেষ (কমিট ও Actions যাচাই আপনার হাতে)
- রিপোজিটরি: 29arafat/arafat-FX-agent (পাবলিক), শাখা: main (data ধাপ ৫-এ)
- ডেলিভারি: zip আপলোড + scripts/apply_zip.sh (সরাসরি কমিট সম্ভব হয়নি)
- Secrets আছে: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, PRICE_API_KEY (Twelve Data)
- বট: @arafatFXagentbot, ব্যক্তিগত চ্যাট (গ্রুপে পরে Secret বদলে)
- যাচাই করা তথ্য (২০২৬-১০-২৩): Twelve Data ফ্রি ৮/মিনিট, ৮০০/দিন;
  XAU/USD H1, H4 ফ্রি Key-তে চলে; ৫০০০ ক্যান্ডেল/কল (H4 ~২.৭ বছর)
- ডেটা নিয়ম: timezone=UTC সবসময়; সর্বশেষ ক্যান্ডেল চলমান, বাদ; datetime = শুরু
- D1: প্রোভাইডারের D1 ২০:০০-২০:০০ UTC। নিজে H4 থেকে বানাব, config:
  d1_day_start_hour (এখন ০)। বাকি: Exness D1 Open (২২ অক্টো.) ~4125.8→০, ~4134.2→২০
- সিদ্ধান্ত: ৬ পেয়ার; H1/H4/D1 স্বতন্ত্র; Running পেয়ার+টাইমফ্রেম প্রতি ১;
  ট্র্যাকিং H1-এ; ইন্ডিকেটর নিজে লেখা; provider adapter; heartbeat; notified
  ফ্ল্যাগ; ব্যাকটেস্টে স্প্রেড ও out-of-sample; cron "7 * * * *"; গোল্ড ২ দশমিক
- বাকি যাচাই: ফরেক্স পেয়ার ফ্রি Key-তে, Twelve Data "non-display" শর্ত,
  ইতিহাস কত পেছনে, ৬০ দিনের Actions নিয়ম, Cloudflare Pages বনাম Workers
- জানা সমস্যা / বাকি কাজ: রিপো সুরক্ষা সেটিংস, বটে Start, Codespaces প্রথম চালু
- ফাইল পরিবর্তন (শেষ zip): step1-structure.zip
- পরের পদক্ষেপ: ধাপ ২ (fetch.py ও indicators.py)

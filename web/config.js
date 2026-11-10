// অ্যাপের সেটিংস। কোনো Secret এখানে নয়।
window.FXCFG = {
  dataBase: "https://raw.githubusercontent.com/29arafat/arafat-FX-agent/data/",
  telegramUrl: "https://t.me/arafatFXagentbot",
  refreshMinutes: 5,
  utcOffsetHours: 3,                 // সৌদি সময় (Asia/Riyadh)
  staleHours: 3,                     // শেষ রানের পর এর বেশি গেলে সতর্কতা
  newBadgeHours: 6,
  minTrades: 30,
  decimals: { XAUUSD: 2, EURUSD: 5, GBPUSD: 5, USDJPY: 3, AUDUSD: 5, USDCAD: 5 },
};

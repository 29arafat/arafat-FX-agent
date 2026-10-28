// Arafat FX Agent: ড্যাশবোর্ড। data ব্রাঞ্চের JSON পড়ে, কিছু লেখে না।
// ডেটা ঢোকানো হয় শুধু textContent দিয়ে (কাঁচা HTML স্ট্রিং নয়), তাই JSON-এ যা-ই থাকুক পেজ ভাঙে না।
(function () {
  "use strict";
  var C = window.FXCFG;
  var KEY = "fx-cache-v1";
  var S = { signals: [], status: null, fetchedAt: null, error: null, loading: false, period: "all",
            hist: { pair: "all", res: "all", tf: "all" } };

  // ---------- ছোট সহায়ক ----------
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "class") el.className = attrs[k];
      else if (k === "text") el.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] === true) el.setAttribute(k, "");
      else if (attrs[k] !== false && attrs[k] != null) el.setAttribute(k, attrs[k]);
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  var NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    for (var i = 2; i < arguments.length; i++) addNode(el, arguments[i]);
    return el;
  }
  function addNode(el, c) {
    if (!c) return;
    if (Array.isArray(c)) c.forEach(function (x) { addNode(el, x); }); else el.appendChild(c);
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function sa(iso) { return new Date(new Date(iso).getTime() + C.utcOffsetHours * 3600000); }   // সৌদি সময় (UTC+3)
  function fmtTime(iso) {
    if (!iso) return "—";
    var d = sa(iso);
    return pad(d.getUTCDate()) + "/" + pad(d.getUTCMonth() + 1) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }
  function ago(iso) {
    var m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (m < 2) return "এইমাত্র";
    if (m < 60) return m + " মিনিট আগে";
    if (m < 2880) return Math.round(m / 60) + " ঘণ্টা আগে";
    return Math.round(m / 1440) + " দিন আগে";
  }
  function px(pair, v) { var d = C.decimals[pair]; return Number(v).toFixed(d == null ? 5 : d); }
  function rTxt(r, dp) { var x = Number(r); return (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(dp == null ? 2 : dp); }
  function cls(x) { return x > 0 ? "pos" : x < 0 ? "neg" : "zero"; }

  // ---------- পরিসংখ্যান (Python-এর stats.summarize-এর মতো) ----------
  function summarize(list) {
    var rs = list.map(function (s) { return Number(s.result_r); });
    var wins = rs.filter(function (r) { return r > 0; }), losses = rs.filter(function (r) { return r < 0; });
    var gw = wins.reduce(function (a, b) { return a + b; }, 0), gl = -losses.reduce(function (a, b) { return a + b; }, 0);
    var cum = 0, peak = 0, dd = 0;
    rs.forEach(function (r) { cum += r; peak = Math.max(peak, cum); dd = Math.max(dd, peak - cum); });
    return { n: rs.length, wins: wins.length, winRate: rs.length ? wins.length / rs.length * 100 : 0, total: cum,
             exp: rs.length ? cum / rs.length : 0, pf: gl > 0 ? gw / gl : (gw > 0 ? Infinity : 0), dd: dd };
  }
  function closed(list) {
    return list.filter(function (s) { return s.status === "TP" || s.status === "SL"; })
      .sort(function (a, b) { return a.closed_at < b.closed_at ? -1 : 1; });
  }
  function inPeriod(list, period) {
    if (period === "all") return list;
    var from = Date.now() - (period === "7" ? 7 : 30) * 86400000;
    return list.filter(function (s) { return new Date(s.closed_at).getTime() >= from; });
  }
  function running() { return S.signals.filter(function (s) { return s.status === "Running"; }); }
  function pairsList() {
    var set = {};
    Object.keys((S.status && S.status.pairs) || {}).forEach(function (p) { set[p] = 1; });
    S.signals.forEach(function (s) { set[s.pair] = 1; });
    return Object.keys(set).sort();
  }

  // ---------- ডেটা আনা ----------
  function saveCache() {
    try { localStorage.setItem(KEY, JSON.stringify({ signals: S.signals, status: S.status, at: S.fetchedAt })); } catch (e) { /* ব্যক্তিগত মোডে চলবে না, ঠিক আছে */ }
  }
  function loadCache() {
    try {
      var c = JSON.parse(localStorage.getItem(KEY) || "null");
      if (c && Array.isArray(c.signals)) { S.signals = c.signals; S.status = c.status; S.fetchedAt = c.at; return true; }
    } catch (e) { /* ক্যাশ নষ্ট হলে উপেক্ষা */ }
    return false;
  }
  function getJSON(name) {
    return fetch(C.dataBase + name + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(name + " HTTP " + r.status);
      return r.json();
    });
  }
  function refresh() {
    if (S.loading) return Promise.resolve();
    S.loading = true; S.error = null; chrome();
    return Promise.all([getJSON("signals.json"), getJSON("status.json")]).then(function (res) {
      if (!Array.isArray(res[0])) throw new Error("signals.json ফরম্যাট ভুল");
      S.signals = res[0]; S.status = res[1]; S.fetchedAt = new Date().toISOString(); saveCache();
    }).catch(function (e) {
      S.error = e && e.message ? e.message : "অজানা ত্রুটি";
    }).then(function () { S.loading = false; render(); });
  }

  // ---------- শেল: উপরের বার, ব্যানার, মেনু ----------
  function lastCheck() { return S.status && S.status.last_check; }
  function chrome() {
    var ver = S.status && S.status.strategy_version;
    document.getElementById("ver").textContent = ver ? "কৌশল " + ver + " · পরীক্ষামূলক" : "";
    var sync = document.getElementById("sync"), text = document.getElementById("syncText");
    var lc = lastCheck(), hrs = lc ? (Date.now() - new Date(lc).getTime()) / 3600000 : null;
    var state = S.loading ? "busy" : S.error ? "bad" : hrs == null ? "" : hrs > C.staleHours ? "stale" : "ok";
    sync.className = "sync " + state;
    text.textContent = S.loading ? "আপডেট হচ্ছে…" : lc ? "সিস্টেম " + ago(lc) : (S.error ? "ডেটা নেই" : "লোড হচ্ছে");
    var b = document.getElementById("banner");
    b.hidden = true; b.className = "banner"; b.textContent = "";
    if (S.error) {
      b.hidden = false; b.className = "banner bad";
      b.textContent = "ডেটা আনা যায়নি" + (S.fetchedAt ? ", সংরক্ষিত ডেটা দেখাচ্ছি (" + fmtTime(S.fetchedAt) + " সৌদি)" : "") + "। ইন্টারনেট দেখে 'রিফ্রেশ' চাপুন।";
    } else if (hrs != null && hrs > C.staleHours) {
      b.hidden = false;
      b.textContent = "শেষ আপডেট " + ago(lc) + "। সিস্টেম বন্ধ থাকতে পারে। Telegram-এর দৈনিক 'সিস্টেম সচল' বার্তা এসেছে কিনা দেখুন।";
    }
    var n = running().length, c = document.getElementById("runCount");
    c.hidden = n === 0; c.textContent = n;
  }

  // ---------- অংশগুলো ----------
  function chips(options, value, onPick) {
    return h("div", { class: "chips", role: "group" }, options.map(function (o) {
      return h("button", { class: "chip", type: "button", "aria-pressed": String(o[0] === value), text: o[1], onclick: function () { onPick(o[0]); } });
    }));
  }

  function curve(list) {
    var rs = list.map(function (s) { return Number(s.result_r); });
    if (rs.length < 2) {
      return h("div", { class: "curve-empty", text: rs.length ? "একটি সিগন্যালের ফল এসেছে। কার্ভ আঁকতে অন্তত দুটি লাগে।" : "এই সময়ে এখনো কোনো সিগন্যাল বন্ধ হয়নি।" });
    }
    var W = 360, H = 150, T = 12, B = 44, L = 8, R = 8, cum = 0, pts = [0];
    rs.forEach(function (r) { cum += r; pts.push(cum); });
    var lo = Math.min.apply(null, pts.concat(0)), hi = Math.max.apply(null, pts.concat(0));
    if (hi - lo < 2) { hi += 1; lo -= 1; }
    var plotH = H - T - B;
    function X(i) { return L + (W - L - R) * i / (pts.length - 1); }
    function Y(v) { return T + plotH * (hi - v) / (hi - lo); }
    var d = pts.map(function (v, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); }).join(" ");
    var maxAbs = Math.max.apply(null, rs.map(Math.abs)), mid = H - B / 2 - 1, bw = Math.max(1.5, Math.min(8, (W - L - R) / rs.length - 1));
    var bars = rs.map(function (r, i) {
      var hh = Math.max(2, Math.abs(r) / maxAbs * (B / 2 - 3));
      return svg("rect", { x: (X(i + 1) - bw / 2).toFixed(1), y: (r >= 0 ? mid - hh : mid).toFixed(1), width: bw.toFixed(1), height: hh.toFixed(1),
                           rx: "1", fill: r >= 0 ? "#6fc7a0" : "#e8766c" });
    });
    var last = pts[pts.length - 1];
    var el = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "curve", role: "img", "aria-label": "R-এর ধারাবাহিক যোগফলের কার্ভ ও প্রতিটি ট্রেডের ফল" },
      svg("line", { x1: L, x2: W - R, y1: Y(0), y2: Y(0), stroke: "#28414a", "stroke-dasharray": "4 4" }),
      svg("path", { d: d, fill: "none", stroke: "#d4a64a", "stroke-width": "2.4", "stroke-linejoin": "round", "stroke-linecap": "round" }),
      svg("circle", { cx: X(pts.length - 1), cy: Y(last), r: "4.5", fill: "#d4a64a" }),
      svg("line", { x1: L, x2: W - R, y1: mid, y2: mid, stroke: "#28414a" }),
      bars);
    return el;
  }

  function rail(s) {
    var vals = [s.sl, s.tp, s.entry].map(Number);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = hi - lo || 1;
    function pos(v) { return ((Number(v) - lo) / span * 100).toFixed(1); }
    var sell = s.direction === "SELL";
    var entryPct = pos(s.entry) + "%";
    function mark(kind, label, v) {
      return h("div", { class: "mark " + kind, style: "left:" + pos(v) + "%" }, h("b", { class: "num", text: px(s.pair, v) }), h("i"), h("span", { class: "lab", text: label }));
    }
    return h("div", { class: "rail", role: "img", "aria-label": "SL " + px(s.pair, s.sl) + ", এন্ট্রি " + px(s.pair, s.entry) + ", TP " + px(s.pair, s.tp) },
      h("div", { class: "rail-line" + (sell ? " rev" : ""), style: "--p-entry:" + entryPct }),
      mark("sl", "SL", s.sl), mark("en", "এন্ট্রি", s.entry), mark("tp", "TP", s.tp));
  }


  // ---------- ইনফো পপ-আপ: কেন এই সিগন্যাল, ঝুঁকি ও সতর্কতা ----------
  var TFROLE = { H1: ["D1", "H4", "H1"], H4: ["D1", "H4", "H4"], D1: ["D1", "D1", "D1"] };   // [ট্রেন্ড, পুলব্যাক, ট্রিগার] (config.yaml tf_map)
  var MAXSCORE = 8;                                                                             // ২+২+২+১+১ (config.yaml scoring.weights)

  // নতুন সিগন্যালে parts আছে। পুরনো সিগন্যালে স্কোর ও কনফার্মড থেকে যতটা নিশ্চিতভাবে বলা যায় তাই (বাকি null = অজানা)।
  function partsOf(s) {
    if (s.parts && typeof s.parts === "object") {
      return { conf: !!s.parts.confirmation, rsi: !!s.parts.rsi_state, level: !!s.parts.no_opposing_level, known: true };
    }
    var conf = !!s.confirmed, rest = Number(s.score) - 4 - (conf ? 2 : 0), rsi = null, level = null;
    if (rest <= 0) { rsi = false; level = false; }
    else if (rest >= 2) { rsi = true; level = true; }
    return { conf: conf, rsi: rsi, level: level, known: false };
  }

  function sec(title, nodes) { return h("section", { class: "msec" }, h("h3", { text: title }), nodes); }
  function para(t) { return h("p", { text: t }); }
  function mark(v) { return v === true ? ["✓", "ok", "মিলেছে"] : v === false ? ["✗", "no", "মেলেনি"] : ["?", "unk", "রেকর্ড নেই"]; }
  function cond(num, title, pts, v, desc) {
    var m = mark(v);
    return h("li", { class: "cond " + m[1] },
      h("span", { class: "cmark", "aria-label": m[2], text: m[0] }),
      h("div", null, h("b", { text: num + " " + title + " (" + pts + ")" }), h("span", { class: "cst", text: " · " + m[2] }), h("div", { class: "cdesc", text: desc })));
  }
  function bullets(arr) { return h("ul", { class: "warn" }, arr.map(function (t) { return h("li", { text: t }); })); }

  function infoContent(s) {
    var sell = s.direction === "SELL", pa = s.pair, role = TFROLE[s.timeframe] || TFROLE.H1, p = partsOf(s);
    var entry = Number(s.entry), risk = Math.abs(entry - Number(s.sl)), reward = Math.abs(Number(s.tp) - entry);
    var upDown = sell ? "নিচের" : "ওপরের", moveTxt = sell ? "নিচে নামার" : "ওপরে ওঠার";
    var others = S.signals.filter(function (x) { return x.status === "Running" && x.id !== s.id && x.pair === s.pair && x.direction === s.direction; });
    var root = h("div");
    add(root, sec("সংক্ষেপে", [
      para(pa + "-এ " + s.timeframe + " টাইমফ্রেমে " + moveTxt + " (" + s.direction + ") সিগন্যাল। " + role[0] + " চার্টে বড় ট্রেন্ড " + upDown +
           " দিকে। সেই ট্রেন্ডের মধ্যে ছোট উল্টো-নড়াচড়ার (পুলব্যাক) পর আবার ট্রেন্ডের দিকে ফেরার সম্ভাবনা ধরে এই এন্ট্রি।"),
      para("এটা ভবিষ্যদ্বাণী নয়। নির্দিষ্ট কিছু নিয়ম মিলেছে বলেই সিস্টেম সংকেত দিয়েছে, আর তার মধ্যেও হারার সম্ভাবনা সবসময় আছে।")]));
    add(root, sec("কোন লজিকে সিগন্যাল এল", [
      h("ul", { class: "conds" }, [
        cond("১.", "পরিষ্কার ট্রেন্ড (" + role[0] + ")", "+২", true,
             "EMA50 ও EMA200-এর ফাঁক অন্তত ০.৩ × ATR, আর EMA50 " + (sell ? "নিচে" : "ওপরে") + "। এটা বাধ্যতামূলক শর্ত।"),
        cond("২.", "পুলব্যাক জোন (" + role[1] + ")", "+২", true,
             "গত ৬ ক্যান্ডেলের মধ্যে দাম EMA20 পর্যন্ত ফিরেছে, কিন্তু কোনো ক্যান্ডেল EMA50-এর ওপারে ক্লোজ করেনি, অর্থাৎ ট্রেন্ড ভাঙেনি। এটাও বাধ্যতামূলক।"),
        cond("৩.", "কনফার্মেশন (" + role[2] + ")", "+২, ঐচ্ছিক", p.conf,
             "ট্রিগার ক্যান্ডেল ট্রেন্ডের দিকে ক্লোজ হয়েছে, RSI ঘুরেছে, আর কাছের ক্যান্ডেলে RSI মাঝামাঝি (৫০) ছুঁয়েছে। এটা থাকলে সংকেত শক্ত, না থাকলেও সিগন্যাল আসতে পারে।"),
        cond("৪.", "RSI অবস্থা", "+১, ঐচ্ছিক", p.rsi,
             sell ? "RSI ৩৫ থেকে ৬০-এর মধ্যে (খুব নিচে নেমে যায়নি, অর্থাৎ নামার জায়গা আছে)।" : "RSI ৪০ থেকে ৬৫-এর মধ্যে (খুব ওপরে ওঠেনি, অর্থাৎ ওঠার জায়গা আছে)।"),
        cond("৫.", "সামনে বিপরীত লেভেল নেই", "+১, ঐচ্ছিক", p.level,
             "এন্ট্রি থেকে ২R দূরত্বের মধ্যে কাছের " + (sell ? "সাপোর্ট" : "রেজিস্ট্যান্স") + " নেই। এটা মিললে লক্ষ্য ১:৩ হতে পারে (এই সিগন্যালে R:R " + s.rr + ")।")]),
      para("মোট স্কোর " + s.score + "/" + MAXSCORE + " (ন্যূনতম ৪ লাগে)। স্কোর ৬ বা বেশি এবং কনফার্মেশন থাকলে 'কনফার্মড' লেবেল পড়ে" +
           (s.confirmed ? "; এই সিগন্যাল কনফার্মড।" : "; এই সিগন্যাল কনফার্মড নয়, অর্থাৎ অতিরিক্ত শর্ত কম মিলেছে।")),
      (p.known || (p.rsi !== null && p.level !== null)) ? null : para("পুরনো সিগন্যাল, তাই '?' চিহ্নিত শর্তের আলাদা রেকর্ড নেই। যা স্কোর থেকে নিশ্চিত বলা যায় শুধু তাই দেখানো হয়েছে।")]));
    add(root, sec("ট্রেডের হিসাব", [
      h("dl", { class: "kv num" },
        h("dt", { text: "এন্ট্রি" }), h("dd", { text: px(pa, s.entry) }),
        h("dt", { text: "SL (ক্ষতি সীমা)" }), h("dd", { text: px(pa, s.sl) }),
        h("dt", { text: "TP (লক্ষ্য)" }), h("dd", { text: px(pa, s.tp) }),
        h("dt", { text: "ঝুঁকি দূরত্ব" }), h("dd", { text: risk.toFixed(C.decimals[pa] == null ? 5 : C.decimals[pa]) + (s.risk_atr ? " (≈ " + s.risk_atr + " ATR)" : "") }),
        h("dt", { text: "লক্ষ্য দূরত্ব" }), h("dd", { text: reward.toFixed(C.decimals[pa] == null ? 5 : C.decimals[pa]) + " (R:R " + s.rr + ")" }),
        h("dt", { text: "এন্ট্রি বৈধ" }), h("dd", { text: px(pa, s.entry_valid_until) + (sell ? " বা ওপরে" : " বা নিচে") })),
      para("SL: গত ১০ ক্যান্ডেলের সুইং " + (sell ? "হাই" : "লো") + "-এর বাইরে ০.১ ATR বাফার, অথবা ১.৫ ATR, এর মধ্যে যেটা দূরে। SL ৪ ATR-এর বেশি দূরে হলে সিগন্যালই দেওয়া হয় না।"),
      para("TP: ঝুঁকির ২ গুণ (১:২)। সামনে পথ ফাঁকা থাকলে ৩ গুণ (১:৩)। SL হিট মানে −১R, TP হিট মানে +" + (s.rr || "").split(":")[1] + "R (স্প্রেডসহ কিছু কম)।"),
      para("এন্ট্রির সীমা: দাম " + px(pa, s.entry_valid_until) + (sell ? "-এর নিচে" : "-এর ওপরে") + " চলে গেলে ইতিমধ্যে লক্ষ্যের দিকে অনেকটা এগিয়ে গেছে, ঝুঁকি-রিওয়ার্ড খারাপ হয়ে গেছে। তখন ধাওয়া করে ঢুকবেন না।"),
      para("ট্র্যাকিং: সিস্টেম H1 ক্যান্ডেল দিয়ে SL/TP মেলায়। একই ক্যান্ডেলে দুটোই ছুঁলে সাবধানী নিয়মে SL ধরা হয়।")]));
    var w = [
      "কৌশলটি পরীক্ষামূলক (" + (s.strategy_version || "—") + ")। ইতিহাসে এর নির্ভরযোগ্য এজ এখনো প্রমাণিত হয়নি, লাইভ ফরোয়ার্ড টেস্ট চলছে। তাই প্রথমে ডেমো অ্যাকাউন্টে বা খুব ছোট ঝুঁকিতে ব্যবহার করুন।",
      "যেকোনো সিগন্যাল হারতে পারে। লাভ-লস পালা করে আসে, টানা কয়েকটা লসও স্বাভাবিক। একটা ট্রেডের ফল দিয়ে কৌশল বিচার করা যায় না।",
      "এটা ট্রেন্ড-ফলো কৌশল। বাজার ট্রেন্ড ছেড়ে রেঞ্জে গেলে বা ট্রেন্ড উল্টে গেলে বারবার SL হতে পারে।",
      "বড় খবরের (NFP, FOMC, CPI, ফেড/ECB বক্তব্য) আগে-পরে স্প্রেড বাড়ে, দাম লাফিয়ে SL পেরিয়ে যেতে পারে (স্লিপেজ)। খবরের সময় এই ট্রেড এড়ানো বা ছোট করা ভালো।",
      "সিগন্যালের দাম ডেটা-প্রোভাইডারের চার্টের। আপনার ব্রোকারের দামে সামান্য ফারাক ও স্প্রেড থাকবে, বাস্তব এন্ট্রি, SL ও TP কিছুটা আলাদা হবে।",
      "সিগন্যাল ক্যান্ডেল ক্লোজের পরে তৈরি হয়, বার্তা আসতে মিনিট থেকে ঘণ্টাখানেক দেরি হতে পারে। দেরিতে দাম এন্ট্রি-সীমা পার করলে ট্রেডটি এড়ান।",
      "শুক্রবার সন্ধ্যা ও সপ্তাহান্ত: শুক্র ১৮:০০ UTC (সৌদি ২১:০০)-এর পর নতুন সিগন্যাল আসে না। কিন্তু সপ্তাহান্তে ট্রেড খোলা রাখলে সোমবারের গ্যাপে SL পেরিয়ে বড় ক্ষতি হতে পারে।",
      "লট এমনভাবে ঠিক করুন যাতে SL হিটে আপনার সহনীয় অঙ্কই যায়। SL দূরত্ব ছোট-বড় হয়, তাই প্রতিবার একই লট ব্যবহার করলে টাকার ঝুঁকি বদলে যায়। এটি আর্থিক পরামর্শ নয়, সিদ্ধান্ত আপনার।"];
    if (!p.conf) w.unshift("এই সিগন্যালে কনফার্মেশন নেই (শুধু ট্রেন্ড ও পুলব্যাক মিলেছে), তাই সংকেত তুলনামূলক দুর্বল। অনেক ট্রেডার এমন সিগন্যালে ছোট ঝুঁকি নেন বা বাদ দেন।");
    if (s.timeframe !== "H1") w.unshift(s.timeframe + " সিগন্যালে SL তুলনামূলক দূরে (এখানে " + risk.toFixed(C.decimals[pa] == null ? 5 : C.decimals[pa]) + ")। একই লটে টাকার ঝুঁকি বেশি, লট কমিয়ে নিন। ট্রেড বন্ধ হতেও বেশি সময় লাগে।");
    if (others.length) w.unshift("একই পেয়ার ও একই দিকে আরও " + others.length + "টি চলমান সিগন্যাল আছে (" + others.map(function (x) { return x.timeframe; }).join(", ") +
      ")। সেগুলো স্বতন্ত্র ট্রেড নয়, একই বাজার-চালের ওপর বাজি। সব ধরলে ঝুঁকি দ্বিগুণ বা তার বেশি হয়ে যায়।");
    add(root, sec("সম্ভাব্য ঝুঁকি ও সতর্কতা", bullets(w)));
    add(root, h("p", { class: "mfoot num", text: s.id + " · খোলা " + fmtTime(s.opened_at) + " সৌদি" }));
    return root;
  }

  var modalState = null;
  function closeInfo() {
    if (!modalState) return;
    document.removeEventListener("keydown", modalState.onKey);
    modalState.bg.remove();
    document.body.classList.remove("modal-open");
    var back = modalState.opener;
    modalState = null;
    if (back && document.body.contains(back)) back.focus();
  }
  function openInfo(s, opener) {
    closeInfo();
    var titleId = "mtitle";
    var closeBtn = h("button", { class: "mclose", type: "button", "aria-label": "বন্ধ করুন", text: "✕", onclick: closeInfo });
    var box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1" },
      h("div", { class: "mhead" },
        h("div", { class: "mtitle", id: titleId }, h("b", { text: s.pair + " · " + s.direction + " · " + s.timeframe }), h("span", { text: "সিগন্যালের বিস্তারিত" })),
        closeBtn),
      h("div", { class: "mbody" }, infoContent(s)));
    var bg = h("div", { class: "modal-bg", onclick: function (e) { if (e.target === bg) closeInfo(); } }, box);
    function onKey(e) {
      if (e.key === "Escape") { closeInfo(); return; }
      if (e.key === "Tab") {                      // ফোকাস পপ-আপের ভেতরেই থাকে
        var f = box.querySelectorAll("button, a[href]");
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener("keydown", onKey);
    document.body.appendChild(bg);
    document.body.classList.add("modal-open");
    modalState = { bg: bg, onKey: onKey, opener: opener };
    box.focus();
  }

  function sigCard(s, extra) {
    var isNew = s.status === "Running" && Date.now() - new Date(s.opened_at).getTime() < C.newBadgeHours * 3600000;
    return h("article", { class: "sig" },
      h("div", { class: "sig-head" },
        h("span", { class: "sig-pair", text: s.pair }),
        h("span", { class: "tag " + (s.direction === "BUY" ? "buy" : "sell"), text: s.direction === "BUY" ? "BUY" : "SELL" }),
        h("span", { class: "tag", text: s.timeframe }),
        s.confirmed ? h("span", { class: "tag conf", text: "কনফার্মড" }) : null,
        isNew ? h("span", { class: "tag new", text: "নতুন" }) : null,
        h("button", { class: "info-btn", type: "button", "aria-label": "এই সিগন্যালের বিস্তারিত তথ্য", "aria-haspopup": "dialog",
                      onclick: function (e) { openInfo(s, e.currentTarget); } },
          svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, [svg("circle", { cx: "12", cy: "12", r: "9" }), svg("path", { d: "M12 11v5.5" }), svg("circle", { cx: "12", cy: "7.8", r: "0.6" })]))),
      h("div", { class: "sig-meta", text: "খোলা " + fmtTime(s.opened_at) + " সৌদি · " + ago(s.opened_at) }),
      rail(s),
      h("div", { class: "sig-foot" },
        h("span", { class: "num", text: "R:R " + s.rr + " · স্কোর " + s.score }),
        h("span", { class: "num", text: "এন্ট্রি বৈধ " + px(s.pair, s.entry_valid_until) + " পর্যন্ত" })),
      extra || null);
  }

  var PAGE = 30, shown = PAGE;
  function histRows(list) {
    if (!list.length) return h("div", { class: "empty" }, h("b", { text: "কিছু পাওয়া যায়নি" }), "ফিল্টার বদলে দেখুন।");
    var sorted = list.slice().sort(function (a, b) { return (b.closed_at || b.opened_at) < (a.closed_at || a.opened_at) ? -1 : 1; });
    var box = h("div", { class: "rows" }, sorted.slice(0, shown).map(function (s) {
      var run = s.status === "Running";
      return h("details", { class: "hrow" },
        h("summary", null,
          h("div", { class: "l" },
            h("div", { class: "t", text: s.pair + " · " + s.direction + " · " + s.timeframe }),
            h("div", { class: "s num", text: fmtTime(s.opened_at) + " সৌদি" })),
          h("div", { class: "r" },
            h("div", { class: "t num " + (run ? "zero" : cls(s.result_r)), text: run ? "চলমান" : rTxt(s.result_r) + "R" }),
            h("div", { class: "s", text: run ? "—" : (s.status === "TP" ? "TP হিট" : "SL হিট") }))),
        h("dl", { class: "detail num" },
          h("dt", { text: "এন্ট্রি" }), h("dd", { text: px(s.pair, s.entry) }),
          h("dt", { text: "SL" }), h("dd", { text: px(s.pair, s.sl) }),
          h("dt", { text: "TP" }), h("dd", { text: px(s.pair, s.tp) }),
          h("dt", { text: "R:R / স্কোর" }), h("dd", { text: s.rr + " / " + s.score + (s.confirmed ? " (কনফার্মড)" : "") }),
          h("dt", { text: "বন্ধ" }), h("dd", { text: run ? "এখনো চলমান" : fmtTime(s.closed_at) + " সৌদি" }),
          s.ambiguous ? h("dt", { text: "নোট" }) : null,
          s.ambiguous ? h("dd", { text: "একই ক্যান্ডেলে TP ও SL দুটোই ছুঁয়েছে, SL ধরা হয়েছে" }) : null,
          h("dt", { text: "আইডি" }), h("dd", { text: s.id }),
          h("dt", { text: "কৌশল" }), h("dd", { text: s.strategy_version || "—" })));
    }));
    if (sorted.length <= shown) return box;
    return h("div", null, box, h("div", { style: "text-align:center;margin-top:12px" },
      h("button", { class: "btn", type: "button", text: "আরও " + Math.min(PAGE, sorted.length - shown) + "টি দেখুন (বাকি " + (sorted.length - shown) + ")",
                    onclick: function () { shown += PAGE; render(); } })));
  }

  function emptyAll() {
    return h("div", { class: "empty" }, h("b", { text: "এখনো কোনো সিগন্যাল নেই" }),
      "নতুন সিগন্যাল এলে Telegram-এ যাবে এবং এখানে দেখা যাবে।", h("div", { style: "margin-top:16px" },
        h("a", { class: "btn", href: C.telegramUrl, target: "_blank", rel: "noopener", text: "Telegram বট খুলুন" })));
  }

  // ---------- পাতা ----------
  function pageDash() {
    var all = closed(S.signals), list = inPeriod(all, S.period), st = summarize(list);
    var v = h("div");
    add(v, chips([["7", "৭ দিন"], ["30", "৩০ দিন"], ["all", "সব"]], S.period, function (p) { S.period = p; render(); }));
    add(v, h("section", { class: "hero", "aria-label": "মোট ফল" },
      h("div", { class: "hero-label", text: "মোট ফল" }),
      h("div", { class: "hero-num num " + cls(st.total) }, rTxt(st.total, 1), h("small", { text: "R" })),
      curve(list),
      list.length > 1 ? h("div", { class: "tape-note", text: "নিচের দাগ: প্রতিটি ট্রেড (সবুজ লাভ, লাল লস)" }) : null));
    add(v, h("div", { class: "grid" },
      h("div", { class: "card" }, h("div", { class: "k", text: "উইন রেট" }), h("div", { class: "v num", text: st.n ? st.winRate.toFixed(0) + "%" : "—" })),
      h("div", { class: "card" }, h("div", { class: "k", text: "সর্বোচ্চ ড্রডাউন" }), h("div", { class: "v num", text: st.n ? st.dd.toFixed(1) + "R" : "—" })),
      h("div", { class: "card" }, h("div", { class: "k", text: "চলমান সিগন্যাল" }), h("div", { class: "v num", text: String(running().length) })),
      h("div", { class: "card" }, h("div", { class: "k", text: "গড় R প্রতি ট্রেড" }),
        h("div", { class: "v num " + cls(st.exp), text: st.n ? rTxt(st.exp) : "—" }))));
    if (!all.length) add(v, emptyAll());
    else if (st.n < C.minTrades) add(v, h("div", { class: "note", text: "এই সময়ে ট্রেড " + st.n + "টি। " + C.minTrades + "-এর কম হলে সংখ্যা ভাগ্যের ওপর নির্ভর করে, ভরসা করার মতো নয়। পরীক্ষামূলক কৌশলে ২০-৩০টি সিগন্যালের আগে নিয়ম বদলানো হবে না।" }));
    add(v, h("h2", { text: "টাইমফ্রেম অনুযায়ী" }));
    add(v, table(["H1", "H4", "D1"], function (tf) { return list.filter(function (s) { return s.timeframe === tf; }); }));
    var ps = pairsList();
    if (ps.length) {
      add(v, h("h2", { text: "পেয়ার অনুযায়ী" }));
      add(v, table(ps, function (p) { return list.filter(function (s) { return s.pair === p; }); }));
    }
    return v;
  }
  function table(keys, pick) {
    return h("div", { class: "tbl" }, h("table", null,
      h("thead", null, h("tr", null, h("th", { text: "" }), h("th", { text: "ট্রেড" }), h("th", { text: "উইন" }), h("th", { text: "মোট R" }))),
      h("tbody", null, keys.map(function (k) {
        var x = summarize(pick(k));
        return h("tr", null, h("td", { text: k }), h("td", { class: "num", text: String(x.n) }),
          h("td", { class: "num", text: x.n ? x.winRate.toFixed(0) + "%" : "—" }),
          h("td", { class: "num " + (x.n ? cls(x.total) : ""), text: x.n ? rTxt(x.total, 1) : "—" }));
      }))));
  }

  function pageRunning() {
    var r = running().sort(function (a, b) { return a.opened_at < b.opened_at ? 1 : -1; });
    var v = h("div", null, h("h2", { text: "চলমান সিগন্যাল" }));
    if (!r.length) {
      add(v, h("div", { class: "empty" }, h("b", { text: "এখন কোনো সিগন্যাল চলছে না" }), "সিস্টেম প্রতি ঘণ্টায় বাজার দেখে। শর্ত মিললে Telegram-এ বার্তা আসবে।"));
      return v;
    }
    r.forEach(function (s) { add(v, sigCard(s)); });
    add(v, h("p", { class: "sub", text: "বর্তমান দাম এখানে দেখানো হয় না। চার্টে দেখুন। SL বা TP ছুঁলে সিস্টেম Telegram-এ জানায়।" }));
    return v;
  }

  function trendArrow(t) {
    return t === "BUY" ? h("span", { class: "arrow pos", text: "↑" }) : t === "SELL" ? h("span", { class: "arrow neg", text: "↓" }) : h("span", { class: "arrow sub", text: "–" });
  }
  function pagePairs() {
    var v = h("div", null, h("h2", { text: "পেয়ার" }));
    var ps = pairsList();
    if (!ps.length) { add(v, emptyAll()); return v; }
    var info = (S.status && S.status.pairs) || {};
    add(v, h("div", { class: "rows" }, ps.map(function (p) {
      var x = summarize(closed(S.signals.filter(function (s) { return s.pair === p; })));
      var run = S.signals.some(function (s) { return s.pair === p && s.status === "Running"; });
      return h("a", { class: "row", href: "#/pair/" + p },
        h("div", { class: "l" }, h("div", { class: "t" }, trendArrow((info[p] || {}).d1_trend), p, run ? h("span", { class: "run-dot", title: "চলমান আছে" }) : null),
          h("div", { class: "s", text: "D1 ট্রেন্ড: " + ((info[p] || {}).d1_trend === "BUY" ? "ঊর্ধ্বমুখী" : (info[p] || {}).d1_trend === "SELL" ? "নিম্নমুখী" : "অস্পষ্ট") })),
        h("div", { class: "r" }, h("div", { class: "t num", text: x.n ? x.winRate.toFixed(0) + "% উইন" : "—" }), h("div", { class: "s num", text: x.n + " ট্রেড · " + (x.n ? rTxt(x.total, 1) + "R" : "—") })));
    })));
    return v;
  }
  function pagePair(p) {
    var list = S.signals.filter(function (s) { return s.pair === p; });
    var info = ((S.status && S.status.pairs) || {})[p] || {};
    var x = summarize(closed(list)), run = list.filter(function (s) { return s.status === "Running"; });
    var v = h("div", null, h("a", { class: "back", href: "#/pairs", text: "← সব পেয়ার" }), h("h1", { text: p }));
    add(v, h("div", { class: "card", style: "margin-top:12px" }, h("dl", { class: "kv num" },
      h("dt", { text: "D1 ট্রেন্ড" }), h("dd", { text: info.d1_trend === "BUY" ? "ঊর্ধ্বমুখী (শুধু BUY)" : info.d1_trend === "SELL" ? "নিম্নমুখী (শুধু SELL)" : "অস্পষ্ট (সিগন্যাল নেই)" }),
      h("dt", { text: "শেষ ক্যান্ডেল" }), h("dd", { text: info.last_candle ? fmtTime(info.last_candle) + " সৌদি" : "—" }),
      h("dt", { text: "ট্রেড / উইন রেট" }), h("dd", { text: x.n + " / " + (x.n ? x.winRate.toFixed(0) + "%" : "—") }),
      h("dt", { text: "মোট R" }), h("dd", { class: cls(x.total), text: x.n ? rTxt(x.total, 1) + "R" : "—" }),
      h("dt", { text: "সর্বোচ্চ ড্রডাউন" }), h("dd", { text: x.n ? x.dd.toFixed(1) + "R" : "—" }))));
    add(v, h("h2", { text: "চলমান" }));
    if (run.length) run.forEach(function (s) { add(v, sigCard(s)); });
    else add(v, h("p", { class: "sub", text: "এখন কোনো সিগন্যাল চলছে না।" }));
    add(v, h("h2", { text: "আগের সব সিগন্যাল" }));
    add(v, histRows(list.filter(function (s) { return s.status !== "Running"; })));
    return v;
  }

  function pageHistory() {
    var v = h("div", null, h("h2", { text: "ইতিহাস" }));
    var f = S.hist, ps = pairsList();
    if (ps.length > 1) add(v, chips([["all", "সব পেয়ার"]].concat(ps.map(function (p) { return [p, p]; })), f.pair, function (x) { f.pair = x; resetPage(); render(); }));
    add(v, chips([["all", "সব ফল"], ["win", "লাভ"], ["loss", "লস"]], f.res, function (x) { f.res = x; resetPage(); render(); }));
    add(v, chips([["all", "সব টাইমফ্রেম"], ["H1", "H1"], ["H4", "H4"], ["D1", "D1"]], f.tf, function (x) { f.tf = x; resetPage(); render(); }));
    var list = S.signals.filter(function (s) {
      return (f.pair === "all" || s.pair === f.pair) && (f.tf === "all" || s.timeframe === f.tf) &&
        (f.res === "all" || (f.res === "win" && s.status === "TP") || (f.res === "loss" && s.status === "SL"));
    });
    add(v, S.signals.length ? histRows(list) : emptyAll());
    add(v, h("h2", { text: "সেটিংস ও তথ্য" }));
    add(v, h("div", { class: "card" }, h("dl", { class: "kv" },
      h("dt", { text: "সিস্টেমের শেষ রান" }), h("dd", { text: lastCheck() ? fmtTime(lastCheck()) + " সৌদি (" + ago(lastCheck()) + ")" : "—" }),
      h("dt", { text: "অ্যাপ ডেটা এনেছে" }), h("dd", { text: S.fetchedAt ? fmtTime(S.fetchedAt) + " সৌদি" : "—" }),
      h("dt", { text: "সময়" }), h("dd", { text: "সৌদি (UTC+3)" }),
      h("dt", { text: "কৌশল" }), h("dd", { text: ((S.status && S.status.strategy_version) || "—") + " (পরীক্ষামূলক)" }))));
    add(v, h("div", { style: "display:flex;gap:10px;flex-wrap:wrap;margin-top:14px" },
      h("a", { class: "btn", href: C.telegramUrl, target: "_blank", rel: "noopener", text: "Telegram বট" }),
      h("button", { class: "btn", type: "button", text: "রিফ্রেশ", onclick: function () { refresh(); } })));
    add(v, h("p", { class: "sub", style: "margin-top:18px", text: "ফোনে ইনস্টল: Chrome মেনু → 'Add to Home screen' (হোম স্ক্রিনে যোগ করুন)। সিগন্যাল শুধু তথ্য, ট্রেডের সিদ্ধান্ত আপনার।" }));
    return v;
  }

  // ---------- রাউটার ----------
  function route() {
    var m = (location.hash || "#/dash").replace(/^#\/?/, "").split("/");
    return { name: m[0] || "dash", arg: m[1] };
  }
  function render() {
    chrome();
    var r = route(), view = document.getElementById("view"), tab = r.name === "pair" ? "pairs" : r.name;
    var page = r.name === "running" ? pageRunning() : r.name === "pairs" ? pagePairs() : r.name === "pair" ? pagePair(decodeURIComponent(r.arg || ""))
      : r.name === "history" ? pageHistory() : pageDash();
    if (!S.fetchedAt && S.loading) page = h("div", { class: "empty" }, h("b", { text: "ডেটা আনা হচ্ছে…" }));
    else if (!S.fetchedAt && S.error) page = h("div", { class: "empty" }, h("b", { text: "ডেটা পাওয়া যায়নি" }), "ইন্টারনেট ঠিক থাকলে 'রিফ্রেশ' চাপুন। নতুন সেটআপে প্রথম রানের পর ডেটা আসে।");
    view.replaceChildren(page);
    document.querySelectorAll("#tabs a").forEach(function (a) {
      if (a.getAttribute("data-tab") === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
  }
  var lastRoute = "";
  function resetPage() { shown = PAGE; }
  window.addEventListener("hashchange", function () {
    resetPage();
    var key = route().name + "/" + (route().arg || "");
    render();
    if (key !== lastRoute) { window.scrollTo(0, 0); lastRoute = key; }
  });

  // ---------- চালু ----------
  document.getElementById("sync").addEventListener("click", function () { refresh(); });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && (!S.fetchedAt || Date.now() - new Date(S.fetchedAt).getTime() > 60000)) refresh();
  });
  setInterval(function () { if (!document.hidden) refresh(); }, C.refreshMinutes * 60000);
  setInterval(chrome, 60000);
  if ("serviceWorker" in navigator && location.protocol.indexOf("http") === 0) {
    navigator.serviceWorker.register("sw.js").catch(function () { /* SW ছাড়াও অ্যাপ চলে */ });
  }
  loadCache();
  render();
  refresh();
  window.__fx = { summarize: summarize, S: S, partsOf: partsOf };   // টেস্টের জন্য
})();

/* Cabinet browser display (host/web_hub.py). ES5 only: old TV browsers, Fire TV Silk, Pi Chromium.
   URL: /?id=den-tv&role=picture   roles: all, control_panel, howtoplay, picture, pictureboxart,
   videoofplay, gallery, keyboard, photoframe (box art + now-playing banner in a game, Immich photos
   when idle). Add &status=1 to show the link state in a corner. */
(function () {
  "use strict";
  var q = parseQuery(location.search);
  var ID = q.id || ("tv-" + Math.floor(Math.random() * 1e6));
  var ROLE = q.role || "all";
  var SHOW_STATUS = q.status === "1";
  var stage = document.getElementById("stage");
  var statusEl = document.getElementById("status");
  var state = null, version = -1, timer = null, rotIndex = 0, rotKey = "", ws = null, wsFails = 0;
  var clockOffset = 0;   // host clock - browser clock (ms); TVs often have a wrong clock

  function parseQuery(s) {
    var out = {}, parts = (s || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < parts.length; i++) {
      if (!parts[i]) continue;
      var kv = parts[i].split("=");
      out[decodeURIComponent(kv[0])] = decodeURIComponent((kv[1] || "").replace(/\+/g, " "));
    }
    return out;
  }
  function qs() { return "id=" + encodeURIComponent(ID) + "&role=" + encodeURIComponent(ROLE); }
  function esc(t) {
    return String(t == null ? "" : t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function setStatus(t) {
    statusEl.textContent = t;
    statusEl.className = SHOW_STATUS ? "" : "hidden";
  }
  function now() { return new Date(new Date().getTime() + clockOffset); }

  /* ---------------- transport: WebSocket, else long-poll ---------------- */
  function onPayload(p) {
    if (!p || typeof p !== "object") return;
    if (p.time) clockOffset = p.time * 1000 - new Date().getTime();
    version = p.v;
    state = p;
    render(true);
  }
  function connectWs() {
    if (!window.WebSocket || wsFails >= 3) { return poll(); }
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    var opened = false;
    try { ws = new WebSocket(proto + location.host + "/ws?" + qs()); } catch (e) { wsFails++; return poll(); }
    ws.onopen = function () { opened = true; wsFails = 0; setStatus(ID + " [" + ROLE + "] live"); };
    ws.onmessage = function (ev) { try { onPayload(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = function () {
      ws = null;
      if (!opened) wsFails++;
      setStatus("reconnecting...");
      setTimeout(connectWs, opened ? 2000 : 4000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }
  function poll() {
    setStatus(ID + " [" + ROLE + "] polling");
    var x = new XMLHttpRequest();
    x.open("GET", "/api/poll?" + qs() + "&v=" + version + "&_=" + new Date().getTime(), true);
    x.timeout = 40000;
    x.onload = function () {
      if (x.status === 200) { try { onPayload(JSON.parse(x.responseText)); } catch (e) {} }
      setTimeout(poll, x.status === 200 ? 50 : 5000);
    };
    x.onerror = x.ontimeout = function () { setStatus("reconnecting..."); setTimeout(poll, 5000); };
    x.send();
  }

  /* ---------------- screen wake lock ---------------- */
  var lock = null;
  function wake() {
    if (navigator.wakeLock && navigator.wakeLock.request && document.visibilityState === "visible") {
      navigator.wakeLock.request("screen").then(function (l) { lock = l; }, function () {});
    }
  }
  document.addEventListener("visibilitychange", function () { if (!lock || lock.released) wake(); });
  document.addEventListener("click", function () {
    wake();
    var el = document.documentElement, fs = el.requestFullscreen || el.webkitRequestFullscreen;
    if (fs && !document.fullscreenElement && !document.webkitFullscreenElement) { try { fs.call(el); } catch (e) {} }
  });

  /* ---------------- rendering ---------------- */
  function content() {
    var msgs = (state && state.messages) || [], m = null;
    for (var i = msgs.length - 1; i >= 0; i--) {
      var c = msgs[i].cmd;
      if (c === "table" || c === "image" || c === "gallery" || c === "idle" || c === "keypad") { m = msgs[i]; break; }
    }
    return m;
  }
  function show(html, cls) {
    stage.innerHTML = '<div class="screen ' + cls + '">' + html + "</div>";
  }
  function schedule(sec) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { rotIndex++; render(false); }, Math.max(3, sec || 10) * 1000);
  }
  /* ---------------- photoframe role ---------------- */
  var photoQ = [], photoBusy = false, photoTimer = null, photoShown = "";
  function fetchPhotos(cb) {
    if (photoBusy) return;
    photoBusy = true;
    var x = new XMLHttpRequest();
    x.open("GET", "/api/photos?n=4&" + qs() + "&_=" + new Date().getTime(), true);
    x.timeout = 30000;
    x.onload = function () {
      photoBusy = false;
      try { var r = JSON.parse(x.responseText); photoQ = photoQ.concat(r.photos || []); } catch (e) {}
      for (var i = 0; i < photoQ.length; i++) { var im = new Image(); im.src = photoQ[i]; }  // preload
      if (cb) cb();
    };
    x.onerror = x.ontimeout = function () { photoBusy = false; if (cb) cb(); };
    x.send();
  }
  function drawPhoto(pf, info, dim) {
    var secs = pf.interval || 30;
    if (!photoTimer) photoTimer = setTimeout(function () { photoTimer = null; nextPhoto(); }, secs * 1000);
    if (photoQ.length < 2) fetchPhotos(photoQ.length ? null : function () { if (photoQ.length) nextPhoto(); });
    if (!photoShown) { if (!photoQ.length) { drawClock(info); return; } photoShown = photoQ.shift(); }
    var u = esc(photoShown), banner = pf.playing ? '<div class="np"><span>NOW PLAYING</span>' + esc(pf.playing) + "</div>" : "";
    show('<div class="bg" style="background-image:url(\'' + u + '\')"></div><img src="' + u + '" alt="">' + banner +
         '<div class="pclock">' + timeText(info) + "</div>", "pic photo" + (dim ? " dim" : ""));
    var img = stage.getElementsByTagName("img")[0];
    img.onerror = function () { photoShown = ""; if (photoQ.length) nextPhoto(); };
  }
  function nextPhoto() {
    if (photoQ.length) photoShown = photoQ.shift();
    if (state && state.photoframe) render(false);
  }
  function renderPhotoframe(info, m) {
    var pf = state.photoframe;
    if (info.night === "sleep") {
      if (pf.quiet === "off") { show("", "off"); return schedule(60); }
      if (pf.quiet === "clock" || !pf.photos) { drawSleep(); return schedule(30); }
      return drawPhoto(pf, info, true);
    }
    if (m && (m.cmd === "image" || m.cmd === "gallery")) {   // a game is running: its box art + banner
      if (photoTimer) { clearTimeout(photoTimer); photoTimer = null; }
      var it = m.cmd === "image" ? m : (m.items || [])[rotIndex % Math.max(1, (m.items || []).length)] || {};
      var url = it.url;
      if (url) {
        var u = esc(url);
        show('<div class="bg" style="background-image:url(\'' + u + '\')"></div><img src="' + u + '" alt="">' +
             '<div class="np"><span>NOW PLAYING</span>' + esc(pf.playing || m.title || it.title || "") + "</div>", "pic photo");
        return schedule(m.interval || 60);
      }
    }
    if (!pf.photos) { if (pf.playing) { show('<div class="np big"><span>NOW PLAYING</span>' + esc(pf.playing) + "</div>", "text"); return schedule(60); } return drawInfo(info, m); }
    if (timer) { clearTimeout(timer); timer = null; }
    drawPhoto(pf, info, false);
  }

  function render(fresh) {
    var info = (state && state.info) || {};
    if (state && state.photoframe) return renderPhotoframe(info, content());
    var m = content();
    var key = m ? JSON.stringify(m) : "none";
    if (fresh && key !== rotKey) { rotKey = key; rotIndex = 0; }
    if (info.night === "sleep") { drawSleep(); return schedule(30); }
    if (!m || info.night === "info") { return drawInfo(info, m); }
    if (m.cmd === "image") return drawImage(m.url, m.title, m.fallback, 60);
    if (m.cmd === "gallery") {
      var items = m.items || [];
      if (!items.length) return drawTable(m.fallback || {});
      var it = items[rotIndex % items.length];
      return drawImage(it.url, it.title || m.title, m.fallback, m.interval || 9);
    }
    if (m.cmd === "table") return drawTable(m);
    if (m.cmd === "idle") return drawIdle(m, info);
    if (m.cmd === "keypad") { show('<div class="text"><h1>KEYPAD</h1><p>Use the cabinet touch keypad</p></div>', "text"); return schedule(30); }
  }
  function drawImage(url, title, fallback, secs) {
    if (!url) return drawTable(fallback || { title: title, cards: [] });
    var u = esc(url);
    show('<div class="bg" style="background-image:url(\'' + u + '\')"></div><img src="' + u + '" alt="">' +
         (title ? '<div class="cap">' + esc(title) + "</div>" : ""), "pic");
    var img = stage.getElementsByTagName("img")[0];
    img.onerror = function () { if (fallback) drawTable(fallback); };
    schedule(secs);
  }
  function drawTable(m) {
    var cards = m.cards || [], h = "";
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      h += '<div class="card ' + esc(c.type || "instructions") + (cards.length === 1 ? " single" : "") + '"><h2>' +
           esc(c.title) + "</h2><p>" + esc(c.text) + "</p></div>";
    }
    show((m.title ? "<h1>" + esc(m.title) + "</h1>" : "") + '<div class="cards">' + h + "</div>", "table");
    schedule(60);
    fitText();
  }
  function fitText() {   // shrink until the cards fit the screen (long LaunchBox descriptions)
    var s = stage.firstChild, size = 100;
    if (!s) return;
    while (s.scrollHeight > window.innerHeight + 2 && size > 45) { size -= 5; s.style.fontSize = size + "%"; zoom(s, size); }
  }
  function zoom(s, pct) { s.style.zoom = pct / 100; }
  function two(n) { return n < 10 ? "0" + n : "" + n; }
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function timeText(info) {
    var d = now(), h = d.getHours(), m = two(d.getMinutes());
    if (info.clock_24h) return two(h) + ":" + m;
    return ((h % 12) || 12) + ":" + m + '<span style="font-size:.35em"> ' + (h < 12 ? "AM" : "PM") + "</span>";
  }
  function drawClock(info) {
    var d = now();
    show('<div class="time">' + timeText(info) + '</div><div class="date">' + DAYS[d.getDay()] + ", " +
         MONTHS[d.getMonth()] + " " + d.getDate() + "</div>", "clock");
  }
  function drawSleep() { show('<div class="time">' + timeText({}) + "</div>", "sleep"); }
  function drawWeather(info) {
    var w = info.weather;
    if (!w) return false;
    var days = "", list = w.days || [];
    for (var i = 0; i < list.length && i < 5; i++) {
      var dd = new Date(list[i].date + "T12:00:00");
      days += '<div class="day"><b>' + DAYS[dd.getDay()].slice(0, 3) + "</b>" + esc(list[i].hi) + "&deg; / " +
              esc(list[i].lo) + "&deg;" + (list[i].rain != null ? "<br>" + esc(list[i].rain) + "%" : "") + "</div>";
    }
    show("<h1>" + esc(info.place || "WEATHER") + '</h1><div class="now">' + esc(w.temp) + "&deg;" + esc(w.unit || "") +
         '</div><div class="cond">' + esc(w.text) + (w.hi != null ? " &middot; H " + esc(w.hi) + "&deg; L " + esc(w.lo) + "&deg;" : "") +
         '</div><div class="days">' + days + "</div>", "weather");
    return true;
  }
  function drawNews(info) {
    var n = info.news;
    if (!n || !n.headlines || !n.headlines.length) return false;
    var li = "";
    for (var i = 0; i < n.headlines.length; i++) li += "<li>" + esc(n.headlines[i]) + "</li>";
    show("<h1>" + esc(info.news_title || "NEWS") + "</h1><ul>" + li + '</ul><div class="src">' + esc(n.title || "") + "</div>", "news");
    return true;
  }
  function drawInfo(info, m) {
    var slides = ["clock", "weather", "news"], k = rotIndex % slides.length, tries = 0, ok = false;
    while (!ok && tries < 3) {
      var s = slides[(k + tries) % 3];
      ok = s === "clock" ? (drawClock(info), true) : s === "weather" ? drawWeather(info) : drawNews(info);
      tries++;
    }
    schedule(15);
  }
  function drawIdle(m, info) {
    var screens = m.screens && m.screens.length ? m.screens : [{ type: "marquee" }, { type: "clock" }];
    // the TV adds weather and news slides to the cabinet's own idle playlist
    var all = screens.slice(0);
    if (info.weather) all.push({ type: "_weather", duration: 15 });
    if (info.news) all.push({ type: "_news", duration: 20 });
    var s = all[rotIndex % all.length], t = s.type;
    if (t === "clock") drawClock(info);
    else if (t === "_weather") drawWeather(info);
    else if (t === "_news") drawNews(info);
    else if (t === "marquee" || t === "anim") {
      show('<div class="cab">' + esc(m.cabinet || info.cabinet || "ARCADE") + '</div><div class="sub">' + esc(m.subtitle || "") + "</div>", "marquee");
    } else if (t === "last_played" || t === "up_next" || t === "choose") {
      show("<h1>" + esc(s.title || "") + "</h1><p>" + esc(m.selected || s.text || "") + "</p>", "text");
    } else {
      show("<h1>" + esc(s.title || "") + "</h1><p>" + esc(s.text || "") + "</p>", "text");
    }
    schedule(s.duration || m.duration || 10);
  }

  setInterval(function () {        // keep the clock ticking on clock / sleep screens
    var el = stage.querySelector && (stage.querySelector(".time") || stage.querySelector(".pclock"));
    if (el && state) el.innerHTML = timeText(state.info || {});
  }, 10000);
  setStatus("connecting...");
  wake();
  connectWs();
})();

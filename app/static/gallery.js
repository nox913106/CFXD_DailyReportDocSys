// 原始圖檔全螢幕檢視
// - 開啟時「符合畫面」：小圖會放大到填滿畫面，大圖縮小到看得到全貌。百分比是相對原圖像素（100% = 原尺寸）。
// - 放大：＋ −、滑鼠滾輪（以游標為中心）、雙指縮放、雙擊（放大 2.5 倍 ↔ 符合畫面），最大 8 倍。
// - 放大後按住拖曳或捲動移動；符合畫面時左右切換（按鈕、方向鍵、手機滑動）。Esc 關閉。
(function () {
  const lb = document.getElementById("lb");
  if (!lb) return;
  const links = Array.from(document.querySelectorAll("a.gal-open"));
  const img = lb.querySelector("[data-lb-img]");
  const stage = lb.querySelector("[data-lb-stage]");
  const title = lb.querySelector("[data-lb-title]");
  const name = lb.querySelector("[data-lb-name]");
  const dl = lb.querySelector("[data-lb-download]");
  const count = lb.querySelector("[data-lb-count]");
  const pct = lb.querySelector("[data-lb-pct]");
  const MAX = 8;
  let i = 0, opener = null, scale = 1, fit = 1;

  const nw = () => img.naturalWidth || 1, nh = () => img.naturalHeight || 1;
  const fitted = () => Math.abs(scale - fit) < 0.001;

  function computeFit() {
    const pad = 16;
    fit = Math.min((stage.clientWidth - pad) / nw(), (stage.clientHeight - pad) / nh(), MAX);
  }
  // 以 stage 上的點 (ax, ay) 為中心縮放，畫面不跳
  function setScale(s, ax, ay) {
    s = Math.min(MAX, Math.max(Math.min(fit, 1) * 0.5, s));
    if (ax == null) { ax = stage.clientWidth / 2; ay = stage.clientHeight / 2; }
    const r = img.getBoundingClientRect(), sr = stage.getBoundingClientRect();
    const fx = (sr.left + ax - r.left) / r.width, fy = (sr.top + ay - r.top) / r.height;
    scale = s;
    img.style.width = Math.round(nw() * s) + "px";
    img.style.height = Math.round(nh() * s) + "px";
    const r2 = img.getBoundingClientRect();
    stage.scrollLeft += (r2.left + fx * r2.width) - (sr.left + ax);
    stage.scrollTop += (r2.top + fy * r2.height) - (sr.top + ay);
    pct.textContent = Math.round(s * 100) + "%";
    pct.parentElement.title = fitted() ? "目前符合畫面" : "回到符合畫面";
  }
  function toFit() {
    img.style.width = img.style.height = "1px"; // 先縮小，量畫面大小時不受捲軸影響
    computeFit();
    setScale(fit);
    stage.scrollLeft = 0;
    stage.scrollTop = 0;
  }

  function show(n) {
    i = (n + links.length) % links.length;
    const a = links[i];
    img.style.width = img.style.height = "";
    img.src = a.href;
    img.alt = a.dataset.name || "";
    title.textContent = a.dataset.title || "";
    name.textContent = a.dataset.name || "";
    dl.href = a.dataset.download;
    count.textContent = (i + 1) + " / " + links.length;
  }
  img.addEventListener("load", () => {
    count.textContent = (i + 1) + " / " + links.length + " · 原圖 " + nw() + " × " + nh() + " px";
    toFit();
  });
  function open(n) {
    opener = document.activeElement;
    lb.hidden = false;
    document.body.classList.add("lb-open");
    show(n);
    lb.querySelector("[data-lb=close]").focus();
  }
  function close() {
    lb.hidden = true;
    img.removeAttribute("src");
    document.body.classList.remove("lb-open");
    if (opener) opener.focus();
  }

  links.forEach((a, n) => a.addEventListener("click", (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey) return; // 另開分頁照常
    e.preventDefault();
    open(n);
  }));
  lb.addEventListener("click", (e) => {
    const act = e.target.closest("[data-lb]");
    if (!act) return;
    const k = act.dataset.lb;
    if (k === "close") close();
    else if (k === "prev") show(i - 1);
    else if (k === "next") show(i + 1);
    else if (k === "in") setScale(scale * 1.5);
    else if (k === "out") setScale(scale / 1.5);
    else if (k === "fit") toFit();
  });
  document.addEventListener("keydown", (e) => {
    if (lb.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "+" || e.key === "=") setScale(scale * 1.5);
    else if (e.key === "-") setScale(scale / 1.5);
    else if (e.key === "0") toFit();
    else if (fitted() && e.key === "ArrowLeft") show(i - 1);
    else if (fitted() && e.key === "ArrowRight") show(i + 1);
  });
  window.addEventListener("resize", () => { if (!lb.hidden && img.naturalWidth) { const was = fitted(); computeFit(); if (was) toFit(); } });

  const point = (e) => { const r = stage.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  // 滾輪：以游標為中心放大縮小
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [x, y] = point(e);
    setScale(scale * (e.deltaY < 0 ? 1.2 : 1 / 1.2), x, y);
  }, { passive: false });
  // 雙擊：放大 2.5 倍（至少原尺寸）↔ 符合畫面
  stage.addEventListener("dblclick", (e) => {
    const [x, y] = point(e);
    if (fitted()) setScale(Math.max(fit * 2.5, 1), x, y); else toFit();
  });
  // 滑鼠拖曳移動
  let drag = null;
  stage.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, l: stage.scrollLeft, t: stage.scrollTop, moved: false };
    stage.classList.add("drag");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    stage.scrollLeft = drag.l - (e.clientX - drag.x);
    stage.scrollTop = drag.t - (e.clientY - drag.y);
  });
  window.addEventListener("mouseup", () => { drag = null; stage.classList.remove("drag"); });

  // 觸控：雙指縮放；符合畫面時左右滑動切換
  let pinch = null, sx = null, sy = 0;
  stage.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      const [a, b] = e.touches, r = stage.getBoundingClientRect();
      pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), s: scale,
                x: (a.clientX + b.clientX) / 2 - r.left, y: (a.clientY + b.clientY) / 2 - r.top };
      sx = null;
    } else if (e.touches.length === 1) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }
  }, { passive: true });
  stage.addEventListener("touchmove", (e) => {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();
    const [a, b] = e.touches;
    setScale(pinch.s * Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) / pinch.d, pinch.x, pinch.y);
  }, { passive: false });
  stage.addEventListener("touchend", (e) => {
    if (e.touches.length < 2) pinch = null;
    if (sx == null || !fitted()) { sx = null; return; }
    const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    sx = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) show(i + (dx < 0 ? 1 : -1));
  });
})();

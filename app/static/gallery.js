// 原始圖檔全螢幕檢視：左右切換（按鈕、方向鍵、滑動），點圖或「1:1」切換原尺寸（可捲動看細節），Esc 關閉。
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
  let i = 0, opener = null;

  function show(n) {
    i = (n + links.length) % links.length;
    const a = links[i];
    stage.classList.remove("full");
    img.src = a.href;
    img.alt = a.dataset.name || "";
    title.textContent = a.dataset.title || "";
    name.textContent = a.dataset.name || "";
    dl.href = a.dataset.download;
    count.textContent = (i + 1) + " / " + links.length;
  }
  function size() {
    if (img.naturalWidth) count.textContent = (i + 1) + " / " + links.length + " · " + img.naturalWidth + " × " + img.naturalHeight + " px";
  }
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
  function zoom(ev) {
    const full = !stage.classList.contains("full");
    // 以點擊位置為中心切換到原尺寸
    const r = img.getBoundingClientRect();
    const fx = ev && ev.clientX ? (ev.clientX - r.left) / r.width : 0.5;
    const fy = ev && ev.clientY ? (ev.clientY - r.top) / r.height : 0.5;
    stage.classList.toggle("full", full);
    if (full) {
      stage.scrollLeft = img.naturalWidth * fx - stage.clientWidth / 2;
      stage.scrollTop = img.naturalHeight * fy - stage.clientHeight / 2;
    }
  }

  links.forEach((a, n) => a.addEventListener("click", (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey) return; // 另開分頁照常
    e.preventDefault();
    open(n);
  }));
  img.addEventListener("load", size);
  img.addEventListener("click", zoom);
  lb.addEventListener("click", (e) => {
    const act = e.target.closest("[data-lb]");
    if (!act) { if (e.target === stage) close(); return; }
    const k = act.dataset.lb;
    if (k === "close") close();
    else if (k === "prev") show(i - 1);
    else if (k === "next") show(i + 1);
    else if (k === "zoom") zoom();
  });
  document.addEventListener("keydown", (e) => {
    if (lb.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft" && !stage.classList.contains("full")) show(i - 1);
    else if (e.key === "ArrowRight" && !stage.classList.contains("full")) show(i + 1);
  });
  // 手機：左右滑動切換（原尺寸時是捲動，不切換）
  let sx = null, sy = 0;
  stage.addEventListener("touchstart", (e) => { if (e.touches.length === 1) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; } }, { passive: true });
  stage.addEventListener("touchend", (e) => {
    if (sx == null || stage.classList.contains("full")) { sx = null; return; }
    const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    sx = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) show(i + (dx < 0 ? 1 : -1));
  });
})();

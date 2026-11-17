// 版本紀錄頁：升級進行中每 3 秒更新進度與記錄；升級時網頁服務會重新啟動，連不上時繼續等，完成後重新整理頁面。
(function () {
  const card = document.getElementById("upgrade");
  if (!card) return;
  const box = card.querySelector(".upgrade-progress");
  const msg = card.querySelector("[data-up-msg]");
  const log = card.querySelector("[data-up-log]");
  let watching = /等待主機開始|升級中/.test(msg.textContent);
  let down = 0;
  async function poll() {
    try {
      const r = await fetch(card.dataset.status, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      const s = await r.json();
      down = 0;
      box.hidden = false;
      if (log && s.log) { log.textContent = s.log; log.scrollTop = log.scrollHeight; }
      if (s.pending) msg.textContent = "已送出升級要求，等待主機開始…";
      else if (s.state === "running") msg.textContent = "升級中（" + s.at + " 開始）…";
      else if (s.state === "done") {
        msg.textContent = "升級完成：v" + s.from + " → v" + s.to + "，重新載入頁面…";
        setTimeout(() => location.reload(), 1500);
        return;
      } else if (s.state === "error") { msg.textContent = "升級失敗（" + s.at + "），請看下方記錄"; return; }
    } catch (e) {
      down += 1;
      msg.textContent = "服務重新啟動中，等待恢復連線…（" + down * 3 + " 秒）";
    }
    setTimeout(poll, 3000);
  }
  if (watching) setTimeout(poll, 2000);
})();

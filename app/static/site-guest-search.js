(function () {
  const root = document.querySelector("[data-guest-search]");
  if (!root) return;

  const statusUrl = root.dataset.statusUrl;
  const pending = root.querySelector("[data-search-pending]");
  const result = root.querySelector("[data-search-result]");
  const progressValue = root.querySelector("[data-progress-value]");
  const progressBar = root.querySelector("[data-progress-bar]");
  const heading = root.querySelector("[data-result-heading]");
  const terminal = new Set(["ready", "delivered", "completed", "failed"]);

  function pluralizePlots(value) {
    const mod10 = value % 10;
    const mod100 = value % 100;
    if (mod10 === 1 && mod100 !== 11) return "участок";
    if (mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14)) return "участка";
    return "участков";
  }

  async function refresh() {
    try {
      const response = await fetch(statusUrl, {
        credentials: "same-origin",
        headers: {Accept: "application/json"},
      });
      if (!response.ok) return;
      const payload = await response.json();
      const progress = Math.max(0, Math.min(100, Number(payload.progress) || 0));
      progressValue.textContent = `${progress}%`;
      progressBar.style.width = `${progress}%`;
      if (!terminal.has(payload.status)) {
        window.setTimeout(refresh, 2500);
        return;
      }
      pending.hidden = true;
      result.hidden = false;
      if (payload.candidate_count > 0) {
        heading.textContent = `Найдено ${payload.candidate_count} ${pluralizePlots(payload.candidate_count)}`;
      } else if (payload.status === "failed") {
        heading.textContent = "Поиск пока не завершён";
      } else {
        heading.textContent = "Варианты требуют дополнительной проверки";
      }
    } catch (_error) {
      window.setTimeout(refresh, 5000);
    }
  }

  if (!result.hidden) pending.hidden = true;
  else refresh();
})();

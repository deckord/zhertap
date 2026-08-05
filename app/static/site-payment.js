(function () {
  const panel = document.querySelector("[data-payment-panel]");
  if (!panel) return;
  const statusEl = panel.querySelector("[data-payment-status]");
  const providerEl = panel.querySelector("[data-provider-status]");
  const currentPaymentId = panel.dataset.currentPaymentId || "";
  const currentPaymentUrl = panel.dataset.currentPaymentUrl || "";
  let isReloading = false;

  function reloadSoon(delay) {
    if (isReloading) return;
    isReloading = true;
    window.setTimeout(() => window.location.reload(), delay);
  }

  async function checkStatus() {
    try {
      const response = await fetch("/cabinet/payment/status", {
        headers: {"Accept": "application/json"},
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const payload = await response.json();
      if (statusEl && payload.access_label) {
        statusEl.textContent = payload.access_label;
        statusEl.className = `status-pill ${payload.paid ? "access-paid" : "access-free"}`;
      }
      if (providerEl && payload.provider_status) {
        providerEl.textContent = payload.provider_status;
      }
      if (payload.paid) {
        reloadSoon(500);
        return;
      }
      if (
        payload.payment_id &&
        (payload.payment_id !== currentPaymentId || (payload.payment_url || "") !== currentPaymentUrl)
      ) {
        reloadSoon(500);
      }
    } catch (_error) {
      // Next interval will retry.
    }
  }

  checkStatus();
  window.setInterval(checkStatus, 3000);
})();

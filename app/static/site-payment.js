(function () {
  const panel = document.querySelector("[data-payment-panel]");
  if (!panel) return;
  const statusEl = panel.querySelector("[data-payment-status]");
  const providerEl = panel.querySelector("[data-provider-status]");
  const currentAccessPaid = panel.dataset.currentAccessPaid === "true";
  const modal = document.querySelector("[data-payment-modal]");
  const modalTitle = modal?.querySelector("#payment-modal-title");
  const modalCaption = modal?.querySelector("[data-payment-modal-caption]");
  const modalLoading = modal?.querySelector("[data-payment-modal-loading]");
  const modalQr = modal?.querySelector("[data-payment-modal-qr]");
  const modalAmount = modal?.querySelector("[data-payment-modal-amount]");
  const modalError = modal?.querySelector("[data-payment-modal-error]");
  const modalKaspi = modal?.querySelector("[data-payment-modal-kaspi]");
  const csrfToken = document.querySelector("input[name='csrf_token']")?.value || "";
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
      if (payload.paid && !currentAccessPaid) {
        reloadSoon(500);
        return;
      }
      if (
        payload.payment_id &&
        (payload.payment_id !== (panel.dataset.currentPaymentId || "") || (payload.payment_url || "") !== (panel.dataset.currentPaymentUrl || ""))
      ) {
        reloadSoon(500);
      }
    } catch (_error) {
      // Next interval will retry.
    }
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("payment-modal-open");
  }

  async function openPaymentModal(plan, label) {
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add("payment-modal-open");
    if (modalTitle) modalTitle.textContent = `Оплата: ${label}`;
    if (modalCaption) modalCaption.textContent = "Создаём безопасный счёт Kaspi для выбранного тарифа.";
    if (modalLoading) modalLoading.hidden = false;
    if (modalQr) { modalQr.hidden = true; modalQr.removeAttribute("src"); }
    if (modalAmount) modalAmount.textContent = "";
    if (modalError) modalError.hidden = true;
    if (modalKaspi) { modalKaspi.hidden = true; modalKaspi.removeAttribute("href"); }
    try {
      const response = await fetch("/cabinet/payment/start-json", {
        method: "POST",
        headers: {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        credentials: "same-origin",
        body: new URLSearchParams({csrf_token: csrfToken, plan}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Не удалось создать счёт");
      if (modalLoading) modalLoading.hidden = true;
      if (modalQr && payload.payment_qr) { modalQr.src = payload.payment_qr; modalQr.hidden = false; }
      if (modalAmount) modalAmount.textContent = `${payload.amount} ₸`;
      if (modalKaspi && payload.payment_url) { modalKaspi.href = payload.payment_url; modalKaspi.hidden = false; }
      panel.dataset.currentPaymentId = payload.payment_id || "";
      panel.dataset.currentPaymentUrl = payload.payment_url || "";
    } catch (error) {
      if (modalLoading) modalLoading.hidden = true;
      if (modalError) { modalError.textContent = error.message || "Не удалось создать счёт"; modalError.hidden = false; }
    }
  }

  document.querySelectorAll("[data-payment-plan]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      openPaymentModal(link.dataset.paymentPlan || "lite", link.textContent.trim());
    });
  });
  document.querySelectorAll("[data-payment-modal-close]").forEach((element) => element.addEventListener("click", closeModal));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });

  checkStatus();
  window.setInterval(checkStatus, 3000);
})();

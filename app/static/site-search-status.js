(function () {
  const root = document.querySelector("[data-search-detail]");
  if (!root) return;

  const searchId = root.dataset.searchId;
  const statusLine = document.querySelector("#search-status-line");
  const statusLabel = document.querySelector("#search-status-label");
  const statusMessage = document.querySelector("#search-status-message");
  const progressPanel = document.querySelector("#search-progress-panel");
  const progressBar = document.querySelector("#search-progress-bar");
  const progressValue = document.querySelector("#search-progress-value");
  const updatedAt = document.querySelector("#search-updated-at");
  const candidateCount = document.querySelector("#candidate-count");
  const candidateList = document.querySelector("#candidate-list");
  const urbanPlanStatusCard = document.querySelector("#urban-plan-status-card");
  const urbanPlanStatusTitle = document.querySelector("#urban-plan-status-title");
  const urbanPlanStatusShort = document.querySelector("#urban-plan-status-short");
  const urbanPlanStatus = document.querySelector("#urban-plan-status");
  const genplanReference = document.querySelector("#genplan-reference-link");
  const paymentStatus = document.querySelector("#payment-status");
  const stageList = document.querySelector("#analysis-stage-list");
  const nextBatchForm = document.querySelector("#next-batch-form");
  const explanationPanel = document.querySelector("#search-explanation-panel");
  const explanationTitle = document.querySelector("#search-explanation-title");
  const explanationBody = document.querySelector("#search-explanation-body");
  const explanationNextTitle = document.querySelector("#search-explanation-next-title");
  const explanationNext = document.querySelector("#search-explanation-next");
  let lastPayload = null;
  let showWithoutGenplan = false;

  const paymentLabels = {
    not_requested: "Полный отчет еще не запрошен",
    awaiting_transfer: "Ожидается перевод Kaspi",
    pending_confirmation: "Оплата на проверке",
    paid: "Полный отчет открыт",
    rejected: "Оплата не подтверждена",
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatNumber(value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "нет данных";
    return Number(value).toFixed(digits);
  }

  function formatDistance(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "нет данных";
    return `${Math.round(Number(value))} м`;
  }

  function genplanBadgeHtml(badge) {
    const item = badge || {
      short: "проверка ожидается",
      tone: "neutral",
    };
    return `<span class="genplan-mini-badge genplan-status-${escapeHtml(item.tone || "neutral")}">${escapeHtml(item.short || "проверка ожидается")}</span>`;
  }

  function statusMessageFor(payload) {
    if (payload.is_failed) return payload.message || "Анализ не завершился. Попробуйте запустить проверку еще раз.";
    if (payload.is_running) return payload.message || "Система выполняет многоэтапную проверку территории.";
    if (showWithoutGenplan && (payload.genplan_preview_candidate_count || 0) > 0) {
      return `Показаны ${payload.genplan_preview_candidate_count} мест без фильтра генплана/ПДП. Они нужны только для сверки причины отказа.`;
    }
    if ((payload.candidate_count || 0) > 0) {
      return `Готово: найдено подходящих мест ${payload.candidate_count}. Откройте карточки ниже и проверьте отчет.`;
    }
    return payload.message || "Анализ завершен. По выбранным условиям перспективные места не найдены.";
  }

  function candidateCard(candidate) {
    const card = document.createElement("article");
    card.className = showWithoutGenplan
      ? "candidate-live-card is-genplan-preview"
      : "candidate-live-card";
    const locality = escapeHtml(candidate.locality || "Населенный пункт не указан");
    const locked = Boolean(candidate.locked);
    const nearbyCadastre = locked
      ? "скрыт до оплаты"
      : escapeHtml(candidate.nearby_cadastre || "нет данных");
    const coordinateText = locked
      ? "координаты скрыты до оплаты"
      : `${formatNumber(candidate.latitude, 6)}, ${formatNumber(candidate.longitude, 6)}`;
    const genplanUrl = `/cabinet/searches/${encodeURIComponent(searchId)}/genplan-map?lat=${encodeURIComponent(
      formatNumber(candidate.latitude, 6),
    )}&lon=${encodeURIComponent(formatNumber(candidate.longitude, 6))}&rank=${encodeURIComponent(
      candidate.rank || "",
    )}`;
    const actionHtml = locked
      ? `
        <button class="locked-action" type="button" disabled>Карта после оплаты</button>
        <button class="locked-action" type="button" disabled>Генплан после оплаты</button>
        <button class="locked-action" type="button" disabled>ЕГКН после оплаты</button>
      `
      : `
        <a href="${escapeHtml(candidate.google_maps_url || "#")}" target="_blank" rel="noopener">Открыть карту</a>
        <a href="${escapeHtml(genplanUrl)}">Генплан</a>
        <a href="${escapeHtml(candidate.egkn_url || "#")}" target="_blank" rel="noopener">Проверить ЕГКН</a>
      `;
    const urbanStatus = genplanBadgeHtml(candidate.urban_plan_badge);
    const urbanZone = candidate.urban_plan_zone ? `<p>${escapeHtml(candidate.urban_plan_zone)}</p>` : "";
    const riskNotes = candidate.risk_notes ? `<p>${escapeHtml(candidate.risk_notes)}</p>` : "";
    const previewNotice = showWithoutGenplan
      ? `<p class="candidate-preview-warning">Показано без фильтра генплана/ПДП: место не подтверждено официальным цифровым слоем для выбранной цели.</p>`
      : "";
    card.innerHTML = `
      <div>
        <strong>#${escapeHtml(candidate.rank)} · ${locality}</strong>
        <span>Перспективность ${formatNumber(candidate.score, 0)}/100 · ${coordinateText}</span>
        ${previewNotice}
        <dl class="candidate-live-facts">
          <div><dt>Кадастровый ориентир</dt><dd>${nearbyCadastre}</dd></div>
          <div><dt>До соседнего участка</dt><dd>${formatDistance(candidate.nearby_distance_m)}</dd></div>
          <div><dt>Дорога</dt><dd>${formatDistance(candidate.road_distance_m)}</dd></div>
          <div><dt>Генплан/ПДП</dt><dd>${urbanStatus}</dd></div>
        </dl>
        ${urbanZone}
        ${riskNotes}
        <p>Оценка показывает приоритет для ручной проверки и не является гарантией выдачи участка.</p>
      </div>
      <div class="candidate-live-actions">
        ${actionHtml}
      </div>
    `;
    return card;
  }

  function hasGenplanPreview(payload) {
    return Boolean(
      payload &&
      payload.report_unlocked &&
      (payload.genplan_preview_candidate_count || 0) > 0
    );
  }

  function selectedCandidates(payload) {
    if (showWithoutGenplan && hasGenplanPreview(payload)) {
      return payload.genplan_preview_candidates || [];
    }
    return payload.candidates || [];
  }

  function updateShowWithoutGenplanControls(payload) {
    const canShow = hasGenplanPreview(payload);
    document.querySelectorAll("[data-show-without-genplan]").forEach((button) => {
      button.classList.toggle("is-hidden", !canShow);
      button.disabled = !canShow;
      if (canShow) {
        button.textContent = showWithoutGenplan
          ? "Показаны без проверки генплана"
          : "Показать без проверки генплана";
      }
    });
  }

  function emptyCandidateHtml(payload) {
    if (payload && payload.is_running) {
      return `
        <strong>Результаты еще не появились</strong>
        <span>Когда worker завершит проверку, найденные места появятся здесь автоматически без обновления страницы.</span>
      `;
    }
    if (payload && payload.urban_plan_status === "blocked") {
      const previewAction = hasGenplanPreview(payload)
        ? `<button class="secondary-action compact" type="button" data-show-without-genplan>Показать без проверки генплана</button>`
        : "";
      return `
        <strong>Подходящих мест по генплану не осталось</strong>
        <span>Кадастровая карта дала предварительные промежутки, но подключенный генплан/ПДП не подтвердил их для выбранной цели. Эти места не показываем как варианты для подачи.</span>
        ${previewAction}
      `;
    }
    return `
      <strong>Подходящие места не найдены</strong>
      <span>Попробуйте другой населенный пункт, район или цель анализа.</span>
    `;
  }

  function renderCandidates(candidates, payload) {
    if (!candidateList) return;
    candidateList.replaceChildren();
    if (!candidates.length) {
      const empty = document.createElement("div");
      empty.className = "empty-live-state";
      empty.innerHTML = emptyCandidateHtml(payload);
      candidateList.append(empty);
      return;
    }
    for (const candidate of candidates) {
      candidateList.append(candidateCard(candidate));
    }
  }

  function updateStages(progress, isFailed) {
    if (!stageList) return;
    const stages = stageList.querySelectorAll("[data-stage-min]");
    stages.forEach((stage) => {
      const min = Number(stage.dataset.stageMin || 0);
      stage.classList.toggle("is-done", progress >= min);
      stage.classList.toggle("is-active", progress >= min && progress < min + 20 && !isFailed);
      stage.classList.toggle("is-failed", Boolean(isFailed) && progress >= min && progress < min + 20);
    });
  }

  function applyStatus(payload) {
    lastPayload = payload;
    if (showWithoutGenplan && !hasGenplanPreview(payload)) showWithoutGenplan = false;
    const progress = Math.max(0, Math.min(100, Number(payload.progress || 0)));
    const label = payload.is_running ? "Выполняется анализ" : payload.status_label;
    if (statusLine) statusLine.textContent = `${label} · прогресс ${progress}%`;
    if (statusLabel) statusLabel.textContent = label;
    if (statusMessage) statusMessage.textContent = statusMessageFor(payload);
    if (progressBar) progressBar.style.width = `${progress}%`;
    if (progressValue) progressValue.textContent = `${progress}%`;
    if (candidateCount) candidateCount.textContent = String(selectedCandidates(payload).length);
    if (payload.urban_plan_badge) {
      if (urbanPlanStatusCard) {
        urbanPlanStatusCard.className = `genplan-status-card genplan-status-${payload.urban_plan_badge.tone || "neutral"}`;
      }
      if (urbanPlanStatusTitle) urbanPlanStatusTitle.textContent = payload.urban_plan_badge.title || "";
      if (urbanPlanStatusShort) urbanPlanStatusShort.textContent = payload.urban_plan_badge.short || "";
      if (urbanPlanStatus) {
        const detail = payload.urban_plan_badge.detail || "";
        urbanPlanStatus.textContent = payload.urban_plan_message
          ? `${detail} ${payload.urban_plan_message}`
          : detail;
      }
    } else if (urbanPlanStatus) {
      urbanPlanStatus.textContent = payload.urban_plan_message
        ? `${payload.urban_plan_status}: ${payload.urban_plan_message}`
        : payload.urban_plan_status;
    }
    if (genplanReference && payload.urban_plan_reference) {
      genplanReference.href = payload.genplan_map_url || genplanReference.href;
      genplanReference.textContent = "Открыть карту генплана";
      if (payload.urban_plan_reference.title) {
        genplanReference.title = payload.urban_plan_reference.title;
      }
    }
    if (paymentStatus) paymentStatus.textContent = paymentLabels[payload.payment_status] || payload.payment_status;
    if (explanationPanel) {
      const explanation = payload.explanation;
      explanationPanel.classList.toggle("is-hidden", !explanation);
      if (explanation) {
        if (explanationTitle) explanationTitle.textContent = explanation.title || "";
        if (explanationBody) explanationBody.textContent = explanation.body || "";
        if (explanationNextTitle) {
          explanationNextTitle.textContent = `${explanation.next_step_title || "Что делать дальше"}:`;
        }
        if (explanationNext) explanationNext.textContent = explanation.next_step || "";
      }
    }
    if (nextBatchForm) {
      nextBatchForm.classList.toggle("is-hidden", !payload.can_request_next_batch);
    }
    if (progressPanel) {
      progressPanel.classList.toggle("is-running", Boolean(payload.is_running));
      progressPanel.classList.toggle("is-failed", Boolean(payload.is_failed));
    }
    if (updatedAt) {
      updatedAt.textContent = payload.is_running
        ? "анализ обновляется автоматически"
        : "анализ завершен";
    }
    updateShowWithoutGenplanControls(payload);
    updateStages(progress, payload.is_failed);
    renderCandidates(selectedCandidates(payload), payload);
    return Boolean(payload.is_running);
  }

  async function fetchStatus() {
    const response = await fetch(`/cabinet/searches/${encodeURIComponent(searchId)}/status`, {
      credentials: "same-origin",
      headers: {Accept: "application/json"},
    });
    if (!response.ok) throw new Error("Не удалось обновить статус");
    return response.json();
  }

  async function tick() {
    try {
      const isRunning = applyStatus(await fetchStatus());
      if (isRunning) window.setTimeout(tick, 3000);
    } catch (error) {
      if (statusMessage) {
        statusMessage.textContent = "Связь со статусом временно потеряна. Пробую обновить анализ снова.";
      }
      if (progressPanel) progressPanel.classList.add("is-failed");
      window.setTimeout(tick, 5000);
    }
  }

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-show-without-genplan]");
    if (!button) return;
    event.preventDefault();
    showWithoutGenplan = true;
    if (lastPayload) {
      applyStatus(lastPayload);
      return;
    }
    try {
      applyStatus(await fetchStatus());
    } catch (error) {
      if (statusMessage) statusMessage.textContent = "Не удалось загрузить места без проверки генплана. Попробуйте еще раз.";
    }
  });

  tick();
})();

(() => {
  const root = document.querySelector("[data-onboarding-tour]");
  if (!root) return;

  const title = root.querySelector("[data-tour-title]");
  const body = root.querySelector("[data-tour-body]");
  const count = root.querySelector("[data-tour-step-count]");
  const progress = root.querySelector("[data-tour-progress]");
  const nextButton = root.querySelector("[data-tour-next]");
  const skipButton = root.querySelector("[data-tour-skip]");
  const helpLink = root.querySelector("[data-tour-help]");
  const dismissTargets = root.querySelectorAll("[data-tour-dismiss]");

  const steps = [
    {
      title: "Добро пожаловать в кабинет",
      body: "Здесь вы запускаете анализ территории, смотрите результаты и возвращаетесь к прошлым проверкам.",
      action: "Начать быстрый тур",
    },
    {
      title: "Новый анализ",
      body: "Нажмите сюда, чтобы выбрать область, район, населенный пункт и цель поиска.",
      target: "a[href='/cabinet/search']",
      action: "Открыть форму",
      nextPath: "/cabinet/search",
    },
    {
      title: "Цель анализа",
      body: "Выберите, что хотите искать. Для ЛПХ и садоводства используются разные размеры участка и разные правила подбора.",
      target: "#search-purpose",
      action: "Дальше",
    },
    {
      title: "Где искать",
      body: "Система берет границы из справочника ЕГКН. Если выбран населенный пункт, ищем внутри него. Если населенный пункт не выбран, проверяем район.",
      target: "#search-region",
      action: "Дальше",
    },
    {
      title: "Проверка по ЕГКН",
      body: "Сначала система смотрит кадастровую карту: где уже есть зарегистрированные участки и есть ли между ними место нужного размера.",
      target: ".analysis-checklist",
      action: "Дальше",
    },
    {
      title: "Проверка окружения",
      body: "Затем система отсеивает места, которые попадают на дороги, здания, воду, кладбища, промзоны и другие заметные объекты на открытой карте.",
      target: ".analysis-checklist",
      action: "Дальше",
    },
    {
      title: "Проверка по генплану",
      body: "Если есть официальный цифровой генплан или ПДП, система проверяет разрешенную зону, красные линии и запретные зоны. Если слоя нет, результат будет помечен как предварительный.",
      target: ".mini-process",
      action: "Дальше",
    },
    {
      title: "Результат",
      body: "В отчете будут координаты, ближайший кадастровый ориентир, расстояния до важных объектов и пояснение, что проверить дальше.",
      target: null,
      action: "Готово",
    },
  ];

  let index = Number(sessionStorage.getItem("zhertapTourStep") || "0");
  if (!Number.isFinite(index) || index < 0 || index >= steps.length) index = 0;
  let highlighted = null;

  const dismiss = async () => {
    root.hidden = true;
    highlighted?.classList.remove("tour-highlight");
    sessionStorage.removeItem("zhertapTourStep");
    try {
      await fetch("/cabinet/onboarding/dismiss", { method: "POST" });
    } catch (_error) {
      localStorage.setItem("zhertapTourDismissed", "1");
    }
  };

  const render = () => {
    const step = steps[index];
    highlighted?.classList.remove("tour-highlight");
    highlighted = step.target ? document.querySelector(step.target) : null;
    if (highlighted) {
      highlighted.classList.add("tour-highlight");
      highlighted.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    root.classList.toggle("has-highlight", Boolean(highlighted));
    title.textContent = step.title;
    body.textContent = step.body;
    count.textContent = `Шаг ${index + 1} из ${steps.length}`;
    progress.style.width = `${((index + 1) / steps.length) * 100}%`;
    nextButton.textContent = step.action;
  };

  if (localStorage.getItem("zhertapTourDismissed") === "1") {
    void dismiss();
    return;
  }

  nextButton.addEventListener("click", () => {
    if (index >= steps.length - 1) {
      void dismiss();
      return;
    }
    const step = steps[index];
    if (step.nextPath && window.location.pathname !== step.nextPath) {
      sessionStorage.setItem("zhertapTourStep", String(index + 1));
      window.location.href = step.nextPath;
      return;
    }
    index += 1;
    sessionStorage.setItem("zhertapTourStep", String(index));
    render();
  });
  skipButton.addEventListener("click", () => void dismiss());
  helpLink?.addEventListener("click", (event) => {
    event.preventDefault();
    const href = helpLink.href;
    void dismiss().finally(() => {
      window.location.href = href;
    });
  });
  dismissTargets.forEach((item) => item.addEventListener("click", () => void dismiss()));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") void dismiss();
  });

  root.hidden = false;
  render();
})();

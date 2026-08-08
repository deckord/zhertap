document.addEventListener("DOMContentLoaded", () => {
  const tabs = document.querySelectorAll("[data-auth-tab]");
  const modes = document.querySelectorAll("[data-auth-mode]");
  const registerPhone = document.querySelector("#register_phone");
  const verifyPhone = document.querySelector("#verify_phone");
  const loginPhone = document.querySelector("#login_phone");
  const resetPhone = document.querySelector("#reset_phone");
  const resetVerifyPhone = document.querySelector("#reset_verify_phone");
  const loginForm = document.querySelector('form[action="/login"]');
  const loginPassword = loginForm?.querySelector('[name="password"]');
  const loginClientError = loginForm?.querySelector("[data-auth-client-error]");

  const setMode = (mode) => {
    tabs.forEach((tab) => {
      tab.classList.toggle("is-active", tab.dataset.authTab === mode);
    });
    modes.forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.authMode === mode);
    });
  };

  loginForm?.addEventListener("submit", (event) => {
    if (loginPhone?.value.trim() && loginPassword?.value) return;
    event.preventDefault();
    if (loginClientError) {
      loginClientError.textContent = "Введите телефон и пароль для входа.";
      loginClientError.hidden = false;
    }
    (loginPhone?.value.trim() ? loginPassword : loginPhone)?.focus();
  });
  loginForm?.addEventListener("input", () => {
    if (loginClientError) loginClientError.hidden = true;
  });

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => setMode(tab.dataset.authTab));
  });

  document.querySelectorAll("[data-auth-switch]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.authSwitch === "reset" && loginPhone?.value && resetPhone) {
        resetPhone.value = loginPhone.value;
        resetVerifyPhone.value = loginPhone.value;
      }
      setMode(button.dataset.authSwitch);
    });
  });

  registerPhone?.addEventListener("input", () => {
    if (verifyPhone) {
      verifyPhone.value = registerPhone.value;
    }
  });

  resetPhone?.addEventListener("input", () => {
    if (resetVerifyPhone) {
      resetVerifyPhone.value = resetPhone.value;
    }
  });

  const query = new URLSearchParams(window.location.search);
  if (document.querySelector(".verify.is-ready") || query.has("invalid")) {
    setMode("register");
  }
  if (
    document.querySelector("[data-auth-mode='reset'].verify.is-ready") ||
    query.has("reset_invalid") ||
    query.has("reset_missing")
  ) {
    setMode("reset");
  }
});

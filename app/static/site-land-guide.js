(() => {
  const button = document.getElementById('land-quiz-check');
  if (!button) return;
  button.addEventListener('click', () => {
    let correct = 0;
    ['land-q1', 'land-q2', 'land-q3'].forEach((name) => {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      if (selected?.value === '1') correct += 1;
    });
    const result = document.getElementById('land-quiz-result');
    result.textContent = `${correct}/3 правильных` + (correct === 3 ? ' — отлично! Можно переходить к заявке.' : ' — повторите шаги выше и попробуйте ещё раз.');
  });
})();

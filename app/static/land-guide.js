(() => {
  const button = document.getElementById('quiz-check');
  if (!button) return;
  const score = document.getElementById('quiz-score');
  const bar = document.getElementById('quiz-progress-bar');
  const result = document.getElementById('quiz-result');
  const answers = ['q1', 'q2', 'q3', 'q4'];
  const grade = () => {
    let correct = 0;
    let answered = 0;
    answers.forEach((name) => {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      if (selected) {
        answered += 1;
        correct += selected.value === '1' ? 1 : 0;
      }
    });
    score.textContent = `${correct}/4`;
    bar.style.width = `${answered * 25}%`;
    result.className = 'quiz-result ' + (correct === 4 ? 'good' : 'retry');
    result.textContent = answered < 4
      ? `Ответьте на все вопросы: заполнено ${answered} из 4.`
      : correct === 4
        ? 'Отлично: маршрут можно использовать в работе.'
        : `Результат ${correct}/4. Повторите шаги 1–5 и проверьте спорные ответы.`;
  };
  button.addEventListener('click', grade);
  answers.forEach((name) => document.querySelectorAll(`input[name="${name}"]`).forEach((input) => input.addEventListener('change', grade)));
})();

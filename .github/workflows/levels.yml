name: Gamma levels

# GitHub часто пропускает запуски по расписанию. Поэтому один запуск работает долго:
# внутри него робот пересчитывает уровни каждые 10 минут, до ~5,5 часа подряд.
# Расписание лишь «будит» его раз в час; пока предыдущий запуск ещё работает,
# новый ждёт в очереди и стартует сразу после него — без дыр в уровнях.
on:
  schedule:
    - cron: "7 * * * 1-5"    # каждый час, пн–пт (время UTC)
  workflow_dispatch: {}      # кнопка «Run workflow» для ручного запуска

permissions:
  contents: write

concurrency:
  group: gamma-levels
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 345
    steps:
      - uses: actions/checkout@v5

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Compute and publish levels every 10 minutes
        run: |
          git config user.name "gamma-levels-bot"
          git config user.email "gamma-levels-bot@users.noreply.github.com"
          start=$(date +%s)
          while :; do
            h=$(date -u +%H); dow=$(date -u +%u)
            # рабочее окно: пн–пт, 06:00–21:59 UTC (01:00–16:59 NY зимой)
            if [ "$dow" -le 5 ] && [ "$h" -ge 6 ] && [ "$h" -le 21 ]; then
              if python gamma_levels.py; then
                git add levels.json
                if ! git diff --cached --quiet; then
                  git commit -q -m "levels $(date -u +%Y-%m-%dT%H:%MZ)"
                  git pull -q --rebase origin main || true
                  git push -q || echo "push failed, next round will retry"
                fi
              else
                echo "robot failed this round, next round will retry"
              fi
            else
              echo "outside working hours"
              break
            fi
            # ~5,5 часа на один запуск, потом его сменит следующий из очереди
            if [ $(( $(date +%s) - start )) -gt 19500 ]; then break; fi
            sleep 600
          done

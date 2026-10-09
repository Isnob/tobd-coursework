# Курсовой проект по ТОБД

Тема: Анализ данных об энергопотреблении для выявления сезонных и поведенческих паттернов использования энергии.

| Файл | Этап CRISP-DM |
|:--|:--|
| `02_sources_study.ipynb` | 2.2 — подбор и изучение источников, аналитический портрет |

## Запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ENTSOE_TOKEN=<токен ENTSO-E>   # необязательно: без него XML ENTSO-E разбирается на примере структуры
jupyter notebook 02_sources_study.ipynb
```

Все загрузки кэшируются в `data/raw/` (каталог не хранится в git). Первый полный запуск скачивает около 300 МБ, основная часть — база OPSD SQLite (226 МБ).

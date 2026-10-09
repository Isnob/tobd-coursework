# Курсовой проект по ТОБД

Тема: Анализ данных об энергопотреблении для выявления сезонных и поведенческих паттернов использования энергии.

| Файл | Этап CRISP-DM |
|:--|:--|
| `02_sources_study.ipynb` | 2.2 — подбор и изучение источников, аналитический портрет |

## Запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ENTSOE_TOKEN=<токен ENTSO-E>   # нужен только для раздела ENTSO-E
jupyter notebook 02_sources_study.ipynb
```

База OPSD (SQLite, 226 МБ) скачивается при первом запуске в `data/` — каталог не хранится в git. Остальные данные загружаются по API за 2024 год.

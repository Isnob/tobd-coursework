"""Этап 2.3: загрузка, очистка, аннотирование, слияние и сохранение данных.

Функции используются в ноутбуке 03_data_preparation.ipynb.
Тот же файл — скрипт обновления базы за новый период:

    python etl.py 2026-01-01 2026-10-21
"""
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DB = Path('data/energy.db')
NEWS_CACHE = Path('data/news')
TZ = 'Europe/Berlin'
CITIES = {'Berlin': (52.52, 13.41), 'Hamburg': (53.55, 9.99), 'Munich': (48.14, 11.58),
          'Frankfurt': (50.11, 8.68), 'Cologne': (50.94, 6.96)}
NEWS_QUERIES = {'strompreis': 'Strompreis', 'negative_preise': '"negative Strompreise"',
                'gaspreis': 'Gaspreis Strompreis'}


def fetch(url, params=None):
    """GET-запрос; при обрыве соединения или ответе 429 (слишком много запросов) ждёт и повторяет."""
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0'}, timeout=300)
            if r.status_code != 429:
                break
        except requests.ConnectionError:
            if attempt == 3:
                raise
        time.sleep(30)
    r.raise_for_status()
    return r


# ---------- Загрузка ----------

def load_price(start, end):
    """Цена рынка на сутки вперёд, зона DE-LU, EUR/МВт·ч."""
    d = fetch('https://api.energy-charts.info/price', {'bzn': 'DE-LU', 'start': start, 'end': end}).json()
    return pd.Series(d['price'], index=pd.to_datetime(d['unix_seconds'], unit='s', utc=True), name='price')


def load_power(start, end):
    """Генерация по типам станций и нагрузка в Германии, МВт."""
    d = fetch('https://api.energy-charts.info/public_power', {'country': 'de', 'start': start, 'end': end}).json()
    return pd.DataFrame({t['name']: t['data'] for t in d['production_types']},
                        index=pd.to_datetime(d['unix_seconds'], unit='s', utc=True)).astype(float)


def load_weather(start, end):
    """Почасовая погода в 5 крупнейших городах Германии."""
    # берём на день раньше: сутки по Берлину начинаются в 23:00 UTC предыдущего дня
    day_before = (pd.Timestamp(start) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    frames = []
    for city, (lat, lon) in CITIES.items():
        d = fetch('https://archive-api.open-meteo.com/v1/archive', {
            'latitude': lat, 'longitude': lon, 'start_date': day_before, 'end_date': end, 'timezone': 'UTC',
            'hourly': 'temperature_2m,wind_speed_100m,shortwave_radiation,cloud_cover,weather_code'}).json()
        frames.append(pd.DataFrame(d['hourly']).assign(city=city))
    weather = pd.concat(frames, ignore_index=True)
    weather['time'] = pd.to_datetime(weather['time'], utc=True)
    return weather


def load_holidays(start, end):
    """Праздники Германии: федеральные и региональные."""
    years = range(int(start[:4]), int(end[:4]) + 1)
    hol = pd.concat([pd.DataFrame(fetch(f'https://date.nager.at/api/v3/PublicHolidays/{y}/DE').json())
                     for y in years], ignore_index=True)
    hol['date'] = pd.to_datetime(hol['date'])
    hol['n_states'] = np.where(hol['global'], 16, hol['counties'].str.len())  # в скольких землях выходной
    return hol[['date', 'localName', 'global', 'n_states']]


def load_eurostat(start, end):
    """Помесячное производство электроэнергии в Германии по видам топлива, ГВт·ч (XML SDMX)."""
    fuels = {'TOTAL': 'total', 'CF': 'fossil', 'RA100': 'hydro', 'RA300': 'wind', 'RA400': 'solar'}
    xml = fetch('https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_cb_pem/'
                'M.TOTAL+CF+RA100+RA300+RA400.GWH.DE',
                {'startPeriod': start[:7], 'endPeriod': end[:7]}).text
    rows = []
    for series in ET.fromstring(xml).findall('.//{*}Series'):
        key = {v.get('id'): v.get('value') for v in series.findall('{*}SeriesKey/{*}Value')}
        for obs in series.findall('{*}Obs'):
            rows.append({'month': obs.find('{*}ObsDimension').get('value'),
                         'fuel': fuels[key['siec']],
                         'gwh': float(obs.find('{*}ObsValue').get('value'))})
    return pd.DataFrame(rows).pivot(index='month', columns='fuel', values='gwh')


def load_news(start, end):
    """Заголовки новостей Google News помесячно по трём запросам.
    Ответы сохраняются в data/news, чтобы не запрашивать их повторно."""
    rows = []
    for key, query in NEWS_QUERIES.items():
        for month in pd.date_range(start, end, freq='MS'):
            path = NEWS_CACHE / key / f'{month:%Y-%m}.xml'
            if not path.exists():
                period = f'after:{month:%Y-%m-%d} before:{month + pd.offsets.MonthBegin():%Y-%m-%d}'
                r = fetch('https://news.google.com/rss/search',
                          {'q': f'{query} {period}', 'hl': 'de', 'gl': 'DE', 'ceid': 'DE:de'})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text)
                time.sleep(1)
            if '<item>' in path.read_text():  # в некоторых месяцах по запросу ничего нет
                items = pd.read_xml(StringIO(path.read_text()), xpath='//item')
                rows.append(items[['title', 'source', 'pubDate', 'link']].assign(query=key))
    return pd.concat(rows, ignore_index=True)


def load_opsd():
    """Цена и нагрузка DE-LU из базы OPSD (SQLite), 10.2018 – 09.2020 — для сверки."""
    con = sqlite3.connect('data/opsd.sqlite')
    opsd = pd.read_sql('''
        SELECT utc_timestamp AS time, DE_LU_price_day_ahead AS price,
               DE_load_actual_entsoe_transparency AS load
        FROM time_series_60min_singleindex
        WHERE DE_LU_price_day_ahead IS NOT NULL''', con)
    con.close()
    return opsd.set_index(pd.to_datetime(opsd.pop('time'), utc=True))


# ---------- Очистка ----------

POWER_COLUMNS = {
    'Load': 'load', 'Residual load': 'residual_load', 'Renewable share of load': 'res_share',
    'Solar': 'solar', 'Wind onshore': 'wind_onshore', 'Wind offshore': 'wind_offshore',
    'Fossil gas': 'gas', 'Fossil brown coal / lignite': 'lignite', 'Fossil hard coal': 'hard_coal',
    'Nuclear': 'nuclear', 'Biomass': 'biomass', 'Hydro Run-of-River': 'hydro',
    'Cross border electricity trading': 'net_import',
}


def clean_price(price):
    # с 01.10.2025 цена публикуется каждые 15 минут — приводим весь ряд к часу
    return price.resample('h').mean()


def clean_power(power):
    # reindex: если ряда нет в выгрузке (например, Nuclear в 2026 г.), столбец будет пустым, а не ошибкой
    power = power.resample('h').mean().reindex(columns=list(POWER_COLUMNS)).rename(columns=POWER_COLUMNS)
    power['nuclear'] = power['nuclear'].fillna(0)  # АЭС закрыты 15.04.2023, после этого ряд не публикуется
    return power


def clean_weather(weather):
    """Погода по зоне: числовые признаки — среднее по 5 городам,
    код погоды — максимальный (наиболее выраженное явление: осадки, снег)."""
    return weather.groupby('time').agg(
        temperature=('temperature_2m', 'mean'), wind_speed_100m=('wind_speed_100m', 'mean'),
        radiation=('shortwave_radiation', 'mean'), cloud_cover=('cloud_cover', 'mean'),
        weather_code=('weather_code', 'max'))


# ---------- Текст: аннотирование ----------

RELEVANT = r'strom|energie|gas|preis|kraftwerk|netz|solar|wind'
TOPICS = {  # тема — по первому совпавшему шаблону
    'газ': r'gas',
    'ВИЭ и отрицательные цены': r'negativ|solar|photovoltaik|wind|erneuerbar|ökostrom|überschuss',
    'биржа': r'börse|großhandel|day-ahead|spotmarkt|terminmarkt|epex',
    'регулирование': r'netzentgelt|umlage|steuer|regierung|gesetz|reform|bundesnetzagentur|habeck|reiche',
    'тарифы для потребителей': r'tarif|anbieter|haushalt|verbraucher|kunden|rechnung|abschlag|vergleich',
}
UP = r'steig|teurer|teuer|rekord|explo|anstieg|verdoppel|höher|hoch'   # рост цен
DOWN = r'sink|günstig|billig|fällt|fallen|niedrig|negativ|entlast|sparen'  # снижение цен


def annotate_news(news):
    news = news.copy()
    news['title'] = news['title'].str.rsplit(' - ', n=1).str[0].str.strip()  # « - Издание» в конце
    news['time'] = pd.to_datetime(news['pubDate'], format='%a, %d %b %Y %H:%M:%S GMT', utc=True)
    news = news.drop_duplicates(['title', 'source'])
    text = news['title'].str.lower()
    news = news[text.str.contains(RELEVANT)].copy()            # убираем нерелевантные заголовки
    text = news['title'].str.lower()
    news['topic'] = 'прочее'
    for topic, pattern in reversed(TOPICS.items()):            # первый в словаре — главный
        news.loc[text.str.contains(pattern), 'topic'] = topic
    news['price_signal'] = (np.sign(text.str.count(UP) - text.str.count(DOWN))).astype(int)
    news['date'] = news['time'].dt.tz_convert(TZ).dt.date
    return news[['time', 'date', 'query', 'source', 'title', 'topic', 'price_signal', 'link']]


def news_daily(news):
    """Агрегат по дням: число публикаций, доли тем, средний ценовой сигнал."""
    daily = news.groupby('date').agg(news_count=('title', 'size'), news_signal=('price_signal', 'mean'))
    shares = pd.crosstab(news['date'], news['topic'], normalize='index')
    shares.columns = ['news_share_' + c for c in shares.columns]
    return daily.join(shares)


# ---------- Слияние ----------

def build_dataset(price, power, weather_zone, holidays, eurostat, news_by_day):
    df = price.to_frame().join(power).join(weather_zone)   # left join: остаются часы, где есть цена
    local = df.index.tz_convert(TZ)

    # календарные признаки — по местному времени
    df['date'] = local.date
    df['hour'] = local.hour
    df['dow'] = local.dayofweek
    df['month'] = local.month
    df['year'] = local.year

    # праздники (день → все часы дня)
    federal = set(holidays.loc[holidays['global'], 'date'].dt.date)
    regional = holidays[~holidays['global']].groupby(holidays['date'].dt.date)['n_states'].sum()
    df['is_holiday'] = df['date'].isin(federal)
    df['holiday_states'] = np.where(df['is_holiday'], 16, df['date'].map(regional).fillna(0))
    df['day_type'] = np.select([df['is_holiday'], df['dow'] >= 5], ['праздник', 'выходной'], 'рабочий')

    # месячные данные Eurostat (месяц → все часы месяца)
    month_key = pd.Series(local.strftime('%Y-%m'), index=df.index)
    df['eu_res_share'] = month_key.map((eurostat['wind'] + eurostat['solar'] + eurostat['hydro']) / eurostat['total'])
    df['eu_fossil_share'] = month_key.map(eurostat['fossil'] / eurostat['total'])

    # новости (день → все часы дня); дни без публикаций — нули
    df = df.join(news_by_day, on='date')
    news_cols = list(news_by_day.columns)
    df[news_cols] = df[news_cols].fillna(0)

    # целевые признаки и режим рынка
    df['is_cheap_window'] = df['price'] <= df.groupby('date')['price'].transform('quantile', 0.25)
    df['market_regime'] = pd.cut(df['year'], [2018, 2020, 2022, 2100],
                                 labels=['до кризиса', 'кризис 2021–22', 'после кризиса'])
    df.index.name = 'time'
    return df


# ---------- Хранение и обновление ----------

def save(df, table, con, key):
    """Записывает строки в таблицу; строки с теми же ключами заменяются (обновление периода)."""
    df = df.reset_index() if df.index.name else df
    for col in df.columns:
        if isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime('%Y-%m-%d')
        elif df[col].dtype == object or isinstance(df[col].dtype, pd.CategoricalDtype):
            df[col] = df[col].astype(str)
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    if exists:
        keys = df[key].drop_duplicates().tolist()
        con.executemany(f'DELETE FROM {table} WHERE {key} = ?', [(k,) for k in keys])
    df.to_sql(table, con, if_exists='append', index=False)
    con.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{key} ON {table}({key})')


def update(start, end):
    """Полный цикл за период: загрузка → очистка → слияние → запись в data/energy.db."""
    news = annotate_news(load_news(start, end))
    holidays = load_holidays(start, end)
    eurostat = load_eurostat(start, end)
    dataset = build_dataset(clean_price(load_price(start, end)), clean_power(load_power(start, end)),
                            clean_weather(load_weather(start, end)), holidays, eurostat, news_daily(news))
    DB.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB) as con:
        save(dataset, 'hourly', con, key='time')
        save(news, 'news', con, key='link')
        save(holidays, 'holidays', con, key='date')
        save(eurostat, 'eurostat_monthly', con, key='month')
    return dataset


if __name__ == '__main__':
    start, end = sys.argv[1], sys.argv[2]
    data = update(start, end)
    print(f'Обновлено {len(data)} часов за {start} – {end} в {DB}')

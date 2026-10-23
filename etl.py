"""Этап 2.3: загрузка, очистка, разметка новостей, слияние и сохранение данных.

Функции используются в ноутбуке 03_data_preparation.ipynb.
Этот же файл — скрипт обновления базы за новый период:

    python etl.py 2026-01-01 2026-10-21
"""
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

DB = 'data/energy.db'
CITIES = {'Berlin': (52.52, 13.41), 'Hamburg': (53.55, 9.99), 'Munich': (48.14, 11.58),
          'Frankfurt': (50.11, 8.68), 'Cologne': (50.94, 6.96)}


def get(url, params=None):
    """Загружает страницу; если сеть оборвалась — ждёт и пробует ещё раз."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0'}, timeout=300)
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(30)
    raise RuntimeError(f'Не удалось загрузить {url}')


# ---------- Загрузка ----------

def load_price(start, end):
    """Цена рынка на сутки вперёд, зона DE-LU, EUR/МВт·ч."""
    d = get('https://api.energy-charts.info/price', {'bzn': 'DE-LU', 'start': start, 'end': end}).json()
    return pd.Series(d['price'], index=pd.to_datetime(d['unix_seconds'], unit='s', utc=True), name='price')


def load_power(start, end):
    """Генерация по типам станций и потребление в Германии, МВт."""
    d = get('https://api.energy-charts.info/public_power', {'country': 'de', 'start': start, 'end': end}).json()
    data = {t['name']: t['data'] for t in d['production_types']}
    return pd.DataFrame(data, index=pd.to_datetime(d['unix_seconds'], unit='s', utc=True)).astype(float)


def load_weather(start, end):
    """Почасовая погода в 5 крупнейших городах Германии."""
    # берём на день раньше: сутки по Берлину начинаются в 23:00 UTC предыдущего дня
    day_before = str(pd.Timestamp(start).date() - pd.Timedelta(days=1))
    frames = []
    for city, (lat, lon) in CITIES.items():
        d = get('https://archive-api.open-meteo.com/v1/archive', {
            'latitude': lat, 'longitude': lon, 'start_date': day_before, 'end_date': end,
            'hourly': 'temperature_2m,wind_speed_100m,shortwave_radiation,cloud_cover,weather_code'}).json()
        frames.append(pd.DataFrame(d['hourly']).assign(city=city))
    weather = pd.concat(frames, ignore_index=True)
    weather['time'] = pd.to_datetime(weather['time'], utc=True)
    return weather


def load_holidays(start, end):
    """Праздники Германии."""
    frames = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        frames.append(pd.DataFrame(get(f'https://date.nager.at/api/v3/PublicHolidays/{year}/DE').json()))
    return pd.concat(frames, ignore_index=True)[['date', 'localName', 'global']]


def load_eurostat(start, end):
    """Помесячное производство электроэнергии в Германии по видам топлива, ГВт·ч (XML)."""
    url = 'https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_cb_pem/M.TOTAL+CF+RA100+RA300+RA400.GWH.DE'
    xml = get(url, {'startPeriod': start[:7], 'endPeriod': end[:7]}).text
    fuels = {'TOTAL': 'total', 'CF': 'fossil', 'RA100': 'hydro', 'RA300': 'wind', 'RA400': 'solar'}
    rows = []
    for series in ET.fromstring(xml).findall('.//{*}Series'):
        key = {v.get('id'): v.get('value') for v in series.findall('{*}SeriesKey/{*}Value')}
        for obs in series.findall('{*}Obs'):
            rows.append({'month': obs.find('{*}ObsDimension').get('value'),
                         'fuel': fuels[key['siec']],
                         'gwh': float(obs.find('{*}ObsValue').get('value'))})
    return pd.DataFrame(rows).pivot(index='month', columns='fuel', values='gwh')


def load_news(start, end):
    """Заголовки Google News помесячно по трём запросам.
    Каждый ответ сохраняется в data/news, чтобы при повторном запуске не скачивать заново."""
    queries = {'strompreis': 'Strompreis', 'negative_preise': '"negative Strompreise"',
               'gaspreis': 'Gaspreis Strompreis'}
    frames = []
    for name, query in queries.items():
        for month in pd.date_range(start, end, freq='MS'):
            path = Path(f'data/news/{name}/{month:%Y-%m}.xml')
            if not path.exists():
                next_month = month + pd.offsets.MonthBegin()
                q = f'{query} after:{month:%Y-%m-%d} before:{next_month:%Y-%m-%d}'
                r = get('https://news.google.com/rss/search', {'q': q, 'hl': 'de', 'gl': 'DE', 'ceid': 'DE:de'})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text)
                time.sleep(1)
            text = path.read_text()
            if '<item>' in text:  # в некоторых месяцах по запросу ничего нет
                frames.append(pd.read_xml(StringIO(text), xpath='//item')[['title', 'source', 'pubDate', 'link']])
    return pd.concat(frames, ignore_index=True)


def load_opsd():
    """Цена и потребление DE-LU из базы OPSD (SQLite), 10.2018 – 09.2020 — для сверки."""
    con = sqlite3.connect('data/opsd.sqlite')
    opsd = pd.read_sql('''
        SELECT utc_timestamp AS time, DE_LU_price_day_ahead AS price,
               DE_load_actual_entsoe_transparency AS load
        FROM time_series_60min_singleindex
        WHERE DE_LU_price_day_ahead IS NOT NULL''', con)
    con.close()
    opsd['time'] = pd.to_datetime(opsd['time'], utc=True)
    return opsd.set_index('time')


# ---------- Очистка ----------

POWER_COLUMNS = {  # нужные ряды генерации и их короткие имена
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
    power = power.resample('h').mean()                  # 15 минут → час
    if 'Nuclear' not in power:                          # после 2023 г. ряда может не быть совсем
        power['Nuclear'] = 0
    power = power[list(POWER_COLUMNS)].rename(columns=POWER_COLUMNS)
    power['nuclear'] = power['nuclear'].fillna(0)       # АЭС закрыты 15.04.2023
    return power


def clean_weather(weather):
    """Погода по зоне: среднее по 5 городам, код погоды — максимальный (самое выраженное явление)."""
    zone = weather.drop(columns='city').groupby('time').mean()
    zone['weather_code'] = weather.groupby('time')['weather_code'].max()
    return zone


# ---------- Новости: очистка и разметка ----------

TOPICS = {  # тема → слова; тема выбирается по первому совпадению
    'газ': ['gas'],
    'ВИЭ и отрицательные цены': ['negativ', 'solar', 'photovoltaik', 'wind', 'erneuerbar', 'ökostrom'],
    'биржа': ['börse', 'großhandel', 'day-ahead', 'spotmarkt', 'epex'],
    'регулирование': ['netzentgelt', 'umlage', 'steuer', 'regierung', 'gesetz', 'reform', 'habeck'],
    'тарифы для потребителей': ['tarif', 'anbieter', 'haushalt', 'verbraucher', 'kunden', 'rechnung'],
}
UP = ['steig', 'teuer', 'rekord', 'explo', 'anstieg', 'verdoppel', 'höher', 'hoch']     # рост цен
DOWN = ['sink', 'günstig', 'billig', 'fällt', 'fallen', 'niedrig', 'negativ', 'sparen']  # снижение цен


def topic_of(title):
    for topic, words in TOPICS.items():
        if any(word in title for word in words):
            return topic
    return 'прочее'


def signal_of(title):
    """+1 — заголовок о росте цен, −1 — о снижении, 0 — нейтральный."""
    up = sum(word in title for word in UP)
    down = sum(word in title for word in DOWN)
    if up > down:
        return 1
    if down > up:
        return -1
    return 0


def annotate_news(news):
    news = news.copy()
    news['title'] = news['title'].str.rsplit(' - ', n=1).str[0]   # убираем « - Издание» в конце
    news = news.drop_duplicates(['title', 'source'])
    lower = news['title'].str.lower()
    news = news[lower.str.contains('strom|energie|gas|preis')]    # только релевантные заголовки
    news['topic'] = news['title'].str.lower().apply(topic_of)
    news['price_signal'] = news['title'].str.lower().apply(signal_of)
    time_utc = pd.to_datetime(news['pubDate'], format='%a, %d %b %Y %H:%M:%S GMT', utc=True)
    news['date'] = time_utc.dt.tz_convert('Europe/Berlin').dt.strftime('%Y-%m-%d')
    return news[['date', 'source', 'title', 'topic', 'price_signal', 'link']]


# ---------- Слияние ----------

def build_dataset(price, power, weather, holidays, eurostat, news):
    df = price.to_frame().join(power).join(weather)   # почасовые данные; остаются часы, где есть цена

    local = df.index.tz_convert('Europe/Berlin')      # календарь — по местному времени
    df['date'] = local.strftime('%Y-%m-%d')
    df['hour'] = local.hour
    df['dow'] = local.dayofweek
    df['month'] = local.month
    df['year'] = local.year

    # праздники: день → все часы дня
    federal = holidays.loc[holidays['global'], 'date']
    df['is_holiday'] = df['date'].isin(federal)
    df['day_type'] = 'рабочий'
    df.loc[df['dow'] >= 5, 'day_type'] = 'выходной'
    df.loc[df['is_holiday'], 'day_type'] = 'праздник'

    # Eurostat: месяц → все часы месяца
    month = pd.Series(local.strftime('%Y-%m'), index=df.index)
    df['eu_res_share'] = month.map((eurostat['wind'] + eurostat['solar'] + eurostat['hydro']) / eurostat['total'])

    # новости: день → все часы дня; дни без новостей — 0
    daily = news.groupby('date').agg(news_count=('title', 'count'), news_signal=('price_signal', 'mean'))
    df = df.join(daily, on='date')
    df[['news_count', 'news_signal']] = df[['news_count', 'news_signal']].fillna(0)

    # целевой признак: час из 25% самых дешёвых за сутки
    df['is_cheap_window'] = df['price'] <= df.groupby('date')['price'].transform('quantile', 0.25)

    # режим рынка: уровень цен сильно отличался до, во время и после энергокризиса
    df['market_regime'] = 'после кризиса'
    df.loc[df['year'] <= 2022, 'market_regime'] = 'кризис 2021–22'
    df.loc[df['year'] <= 2020, 'market_regime'] = 'до кризиса'

    df.index.name = 'time'
    return df.reset_index()


# ---------- Хранение и обновление ----------

def save(df, table, start, end):
    """Записывает данные в таблицу; старые строки за этот же период удаляются,
    поэтому повторный запуск не создаёт дублей."""
    con = sqlite3.connect(DB)
    if con.execute('SELECT 1 FROM sqlite_master WHERE name = ?', (table,)).fetchone():
        con.execute(f'DELETE FROM {table} WHERE date BETWEEN ? AND ?', (start, end))
    df.to_sql(table, con, if_exists='append', index=False)
    con.commit()
    con.close()


def update(start, end):
    """Полный цикл за период: загрузка → очистка → слияние → запись в базу."""
    news = annotate_news(load_news(start, end))
    dataset = build_dataset(clean_price(load_price(start, end)),
                            clean_power(load_power(start, end)),
                            clean_weather(load_weather(start, end)),
                            load_holidays(start, end),
                            load_eurostat(start, end),
                            news)
    save(dataset, 'hourly', start, end)
    save(news, 'news', start, end)
    return dataset


if __name__ == '__main__':
    start, end = sys.argv[1], sys.argv[2]
    data = update(start, end)
    print(f'Записано {len(data)} часов за {start} – {end} в {DB}')

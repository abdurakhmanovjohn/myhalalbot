import aiohttp
import logging
from datetime import date

_cache: dict = {}
_last_good: dict = {}


async def fetch_prayer_times(lat: float, lon: float, method: int = 14, school: int = 1) -> dict | None:
    loc_key = (round(lat, 2), round(lon, 2), method, school)
    today = date.today().isoformat()
    cache_key = (*loc_key, today)

    for k in list(_cache):
        if k[-1] != today:
            del _cache[k]

    if cache_key in _cache:
        return _cache[cache_key]

    url = "https://api.aladhan.com/v1/timings"
    params = {"latitude": lat, "longitude": lon, "method": method, "school": school}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = (await response.json())['data']
                    timings = data['timings']
                    result = {
                        "timezone": data['meta']['timezone'],
                        "timings": {
                            "Fajr": timings["Fajr"],
                            "Sunrise": timings["Sunrise"],
                            "Dhuhr": timings["Dhuhr"],
                            "Asr": timings["Asr"],
                            "Maghrib": timings["Maghrib"],
                            "Isha": timings["Isha"],
                        },
                    }
                    _cache[cache_key] = result
                    _last_good[loc_key] = result
                    return result
                logging.error(f"Aladhan API returned status {response.status}")
    except Exception as e:
        logging.error(f"Error fetching prayer times: {e}")

    fallback = _last_good.get(loc_key)
    if fallback:
        logging.warning("Using cached prayer times as fallback after fetch failure.")
    return fallback
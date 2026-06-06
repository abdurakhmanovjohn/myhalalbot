import aiohttp
import logging

async def fetch_prayer_times(lat: float, lon: float) -> dict | None:
    """
    Fetches the 5 daily prayer times AND timezone from the Aladhan API using coordinates.
    """
    url = "http://api.aladhan.com/v1/timings"
    params = {
        "latitude": lat,
        "longitude": lon,
        "method": 2
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    json_data = await response.json()
                    data = json_data['data']
                    timings = data['timings']
                    timezone = data['meta']['timezone']
                    
                    return {
                        "timezone": timezone,
                        "timings": {
                            "Fajr": timings["Fajr"],
                            "Dhuhr": timings["Dhuhr"],
                            "Asr": timings["Asr"],
                            "Maghrib": timings["Maghrib"],
                            "Isha": timings["Isha"]
                        }
                    }
                else:
                    logging.error(f"Aladhan API returned status {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Error fetching prayer times: {e}")
        return None
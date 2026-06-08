import aiohttp
import logging

async def fetch_prayer_times(lat: float, lon: float, method: int = 2, school: int = 1) -> dict | None:
    url = "https://api.aladhan.com/v1/timings"
    params = {
        "latitude": lat,
        "longitude": lon,
        "method": method,
        "school": school,
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
                            "Sunrise": timings["Sunrise"],
                            "Dhuhr": timings["Dhuhr"],
                            "Asr": timings["Asr"],
                            "Maghrib": timings["Maghrib"],
                            "Isha": timings["Isha"],
                        }
                    }
                else:
                    logging.error(f"Aladhan API returned status {response.status}")
                    return None
                
    except Exception as e:
        logging.error(f"Error fetching prayer times: {e}")
        return None
import aiohttp
import logging

async def fetch_prayer_times(lat: float, lon: float) -> dict | None:
    """
    Fetches the 5 daily prayer times from the Aladhan API using coordinates.
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
                    data = await response.json()
                    timings = data['data']['timings']
                    
                    return {
                        "Fajr": timings["Fajr"],
                        "Dhuhr": timings["Dhuhr"],
                        "Asr": timings["Asr"],
                        "Maghrib": timings["Maghrib"],
                        "Isha": timings["Isha"]
                    }
                else:
                    logging.error(f"Aladhan API returned status {response.status}")
                    return None
                    
    except Exception as e:
        logging.error(f"Error fetching prayer times: {e}")
        return None


if __name__ == "__main__":
    import asyncio
    
    async def test():
        times = await fetch_prayer_times(41.2995, 69.2401)
        print("Fetched Times:", times)

    asyncio.run(test())
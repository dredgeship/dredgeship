"""外部 API 工具组：Open-Meteo 天气查询（加分项 ②）。

选型说明：Open-Meteo 免费、无需申请 API Key、国内可直连，避免了作业环境里
申请密钥与配额限制带来的不确定性。调用分两步：

1. ``geocoding-api.open-meteo.com`` 把中文城市名解析成经纬度；
2. ``api.open-meteo.com`` 按经纬度取当日预报。

失败一律转成可读的中文错误（含网络超时、城市不存在、日期超出预报范围），
让模型能向用户解释而不是崩溃。
"""

from typing import Any

import httpx

from common import timeutil
from mcp_server.tools.base import fail, ok, preference_repo, tool

__all__ = ["register", "fetch_weather", "WeatherError"]

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气代码 -> 中文描述
WEATHER_CODE_TEXT: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "少云",
    3: "阴天",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}

_RAINY_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
_SNOWY_CODES = {71, 73, 75, 77, 85, 86}


class WeatherError(RuntimeError):
    """天气查询失败（网络、城市或日期问题）。"""


def register(mcp: Any) -> None:
    @tool(mcp)
    def get_weather(
        city: str | None = None,
        date: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> str:
        """查询指定城市某一天的天气与出行建议。

        用户提到「天气」「要不要带伞」「适不适合出门办事」时调用。

        Args:
            city: 城市名，例如「杭州」「上海」。不填则依次回退到「用户偏好中的
                默认城市」→「服务端默认城市」。
            date: 日期，默认今天，支持「明天」「后天」「2026-10-16」，
                仅支持未来两周内的日期。
            latitude / longitude: 已知经纬度时可直接按坐标查询（前端浏览器定位
                场景使用）。**一般不需要填**，两者必须同时提供才生效。
        """
        data = fetch_weather(city=city, date=date, latitude=latitude, longitude=longitude)
        message = (
            f"{data['city']} {data['date']}：{data['description']}，"
            f"{data['temperature_min']}~{data['temperature_max']}℃，"
            f"降水概率 {data['precipitation_probability']}%，"
            f"{data['advice']}"
        )
        return ok(message=message, **data)

    @tool(mcp)
    def weather_travel_advice(city: str | None = None, date: str | None = None) -> str:
        """结合天气给出出行安排建议（是否适合外出办事、需要带什么）。

        与 get_weather 的区别：本工具聚焦「今天适不适合出门办事」的结论，
        便于直接回答用户，不必再让模型自行推断。

        Args:
            city: 城市名，不填则使用默认城市。
            date: 日期，默认今天。
        """
        data = fetch_weather(city=city, date=date)
        return ok(
            message=data["advice"],
            city=data["city"],
            date=data["date"],
            description=data["description"],
            advice=data["advice"],
            suggestion=data["outing_suggestion"],
        )


# --------------------------------------------------------------------------- #
# 核心实现
# --------------------------------------------------------------------------- #
def fetch_weather(
    city: str | None = None,
    date: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """查询天气，返回结构化数据；失败抛出 :class:`WeatherError`。

    ``latitude`` / ``longitude`` 同时提供时按坐标查询（跳过地名解析）——
    前端「查天气」按钮拿到浏览器定位后就走这条路径，避免"经纬度 → 猜城市名"
    这一步反查不准。
    """
    has_coords = latitude is not None and longitude is not None
    resolved_city = "当前位置" if has_coords else _resolve_city(city)
    target_day = timeutil.parse_date(date) or timeutil.today()

    delta_days = (target_day - timeutil.today()).days
    if delta_days < -1:
        raise WeatherError("只能查询今天及以后的天气")
    if delta_days > 15:
        raise WeatherError("预报只覆盖未来 15 天，请换一个日期")

    timeout = float(_env_float("WEATHER_TIMEOUT", 10.0))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if has_coords:
                geo = {
                    "name": resolved_city,
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                }
            else:
                geo = _geocode(client, resolved_city)
            forecast = _fetch_forecast(client, geo["latitude"], geo["longitude"], target_day)
    except httpx.TimeoutException as exc:
        raise WeatherError(f"天气服务响应超时（{timeout:g} 秒），请稍后再试") from exc
    except httpx.HTTPError as exc:
        raise WeatherError(f"网络请求失败：{exc}") from exc

    daily = forecast.get("daily") or {}
    if not daily.get("time"):
        raise WeatherError(f"{resolved_city} 在 {timeutil.format_date(target_day)} 没有预报数据")

    code = int(daily["weather_code"][0])
    temp_max = daily["temperature_2m_max"][0]
    temp_min = daily["temperature_2m_min"][0]
    precipitation = daily.get("precipitation_probability_max", [0])[0] or 0
    wind = daily.get("wind_speed_10m_max", [0])[0] or 0
    description = WEATHER_CODE_TEXT.get(code, f"未知天气（代码 {code}）")

    advice, suggestion = _build_advice(code, temp_max, temp_min, precipitation, wind)
    return {
        "city": geo.get("name") or resolved_city,
        "region": " ".join(
            part for part in [geo.get("country"), geo.get("admin1")] if part
        ),
        "date": daily["time"][0],
        "date_human": timeutil.humanize(target_day),
        "description": description,
        "weather_code": code,
        "temperature_max": temp_max,
        "temperature_min": temp_min,
        "precipitation_probability": precipitation,
        "wind_speed_max": wind,
        "advice": advice,
        "outing_suggestion": suggestion,
        "source": "Open-Meteo",
    }


def _resolve_city(city: str | None) -> str:
    if city and str(city).strip():
        return str(city).strip()
    remembered = preference_repo().get("default_city")
    if remembered:
        return str(remembered)
    return _env_str("DEFAULT_CITY", "北京")


def _geocode(client: httpx.Client, city: str) -> dict[str, Any]:
    try:
        response = client.get(
            GEOCODE_URL,
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        response.raise_for_status()
        results = (response.json() or {}).get("results") or []
    except ValueError as exc:
        raise WeatherError("天气服务返回了无法解析的数据") from exc
    if not results:
        raise WeatherError(f"找不到城市「{city}」，请换一个更常见的城市名")
    return results[0]


def _fetch_forecast(
    client: httpx.Client, latitude: float, longitude: float, day
) -> dict[str, Any]:
    response = client.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,wind_speed_10m_max"
            ),
            "timezone": "auto",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        },
    )
    response.raise_for_status()
    return response.json() or {}


def _build_advice(
    code: int,
    temp_max: float,
    temp_min: float,
    precipitation: float,
    wind: float,
) -> tuple[str, str]:
    """返回 ``(给用户的一句话建议, 是否适合外出的结论)``。"""
    notes: list[str] = []
    if code in _RAINY_CODES or precipitation >= 60:
        notes.append("有降水，记得带伞")
    if code in _SNOWY_CODES:
        notes.append("有降雪，注意防滑保暖")
    if temp_max >= 33:
        notes.append("气温偏高，注意补水防暑")
    elif temp_max <= 5:
        notes.append("气温较低，注意保暖")
    if temp_min <= 0:
        notes.append("可能结冰，出行小心")
    if wind >= 40:
        notes.append("风力较大，注意防风")

    if code in _RAINY_CODES or code in _SNOWY_CODES or precipitation >= 70:
        suggestion = "不太适合长时间户外活动，建议把外出事务安排到天气较好的时段"
    elif temp_max >= 35 or wind >= 50:
        suggestion = "户外体验一般，重要外出建议避开正午"
    elif temp_min <= 0:
        suggestion = "可以外出，但注意保暖与路面结冰"
    else:
        suggestion = "天气条件不错，适合外出办事"

    advice = "；".join(notes) + f"。{suggestion}" if notes else suggestion
    return advice, suggestion


def _env_str(key: str, default: str) -> str:
    import os

    value = os.getenv(key, "").strip()
    return value or default


def _env_float(key: str, default: float) -> float:
    import os

    try:
        return float(os.getenv(key, "").strip() or default)
    except ValueError:
        return default

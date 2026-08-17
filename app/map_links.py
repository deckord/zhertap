from urllib.parse import quote


def google_maps_place_url(latitude: float, longitude: float, *, zoom_m: int = 167) -> str:
    label = f"{_dms(latitude, is_latitude=True)} {_dms(longitude, is_latitude=False)}"
    return (
        f"https://www.google.com/maps/place/{quote(label)}"
        f"/@{latitude:.6f},{longitude:.6f},{zoom_m}m/data=!3m1!1e3"
        f"!4m4!3m3!8m2!3d{latitude:.6f}!4d{longitude:.6f}"
    )


def _dms(value: float, *, is_latitude: bool) -> str:
    absolute = abs(value)
    degrees = int(absolute)
    minutes_float = (absolute - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    direction = ("N" if value >= 0 else "S") if is_latitude else ("E" if value >= 0 else "W")
    return f"{degrees}°{minutes:02d}'{seconds:04.1f}\"{direction}"

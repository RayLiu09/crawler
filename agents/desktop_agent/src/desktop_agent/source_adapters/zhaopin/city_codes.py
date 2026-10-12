"""Load reviewed Zhaopin city codes from the bundled or operator-managed JSON file."""

import json
from importlib.resources import files
from pathlib import Path


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, code in pairs:
        if name in values:
            raise ValueError(f"duplicate Zhaopin city name: {name}")
        values[name] = code
    return values


def load_city_codes(path: Path | None = None) -> dict[str, str]:
    source = path.expanduser() if path is not None else files(__package__).joinpath(
        "city_codes.json"
    )
    try:
        data = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Zhaopin city-code file: {source}") from error
    if not isinstance(data, dict) or not data:
        raise ValueError("Zhaopin city-code file must contain a non-empty JSON object")

    codes: dict[str, str] = {}
    used_codes: set[str] = set()
    for city, code in data.items():
        if not city or city != city.strip() or city.endswith("市"):
            raise ValueError(f"invalid Zhaopin city name: {city!r}")
        if not isinstance(code, str) or not code.isascii() or not code.isdecimal():
            raise ValueError(f"invalid Zhaopin city code for {city}")
        if code in used_codes:
            raise ValueError(f"duplicate Zhaopin city code: {code}")
        used_codes.add(code)
        codes[city] = code
        codes[f"{city}市"] = code
    return codes

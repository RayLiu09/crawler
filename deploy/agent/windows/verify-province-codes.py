"""Verify the installed province-code patch without opening a browser."""

import argparse
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from desktop_agent.config import load_agent_settings
from desktop_agent.source_adapters.zhaopin.adapter import (
    ZhaopinBrowserConfig,
    ZhaopinSourceAdapter,
)
from desktop_agent.source_adapters.zhaopin.city_codes import load_city_codes

EXPECTED = {
    "云南省": "554", "广东省": "548", "湖北省": "546", "陕西省": "556",
    "四川省": "552", "辽宁省": "535", "吉林省": "536", "江苏省": "539",
    "山东省": "544", "浙江省": "540", "广西省": "549", "安徽省": "541",
    "河北省": "532", "山西省": "533", "内蒙古自治区": "534", "黑龙江省": "537",
    "福建省": "542", "江西省": "543", "河南省": "545", "湖南省": "547",
    "海南省": "550", "贵州省": "553", "西藏": "555", "甘肃省": "557",
    "青海省": "558", "宁夏回族自治区": "559", "新疆维吾尔自治区": "560",
}
SHORT_NAMES = {
    "内蒙古自治区": "内蒙古",
    "宁夏回族自治区": "宁夏",
    "新疆维吾尔自治区": "新疆",
}


def verify(codes: dict[str, str]) -> None:
    adapter = ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(profile_directory=Path("unused-verification-profile")),
        city_codes=codes,
    )
    keyword = "酒店营销运营专员"
    for name, code in EXPECTED.items():
        short = SHORT_NAMES.get(name, name.removesuffix("省"))
        for region in {name, short}:
            params = parse_qs(urlsplit(adapter.build_search_url(keyword, region, 1)).query)
            if params.get("jl") != [code] or params.get("kw") != [keyword]:
                raise ValueError(f"incorrect search URL for region: {ascii(region)}")
    print("All 27 province codes and their short names verified; no browser started")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    verify(load_city_codes())
    print("Bundled province-code registry verified")
    if args.config is not None:
        settings = load_agent_settings(args.config)
        verify(load_city_codes(settings.zhaopin_city_codes_file))
        print("Effective Agent configuration and province-code registry verified")


if __name__ == "__main__":
    main()

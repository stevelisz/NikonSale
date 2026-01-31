#!/usr/bin/env python3
import argparse
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


@dataclass
class ProductCheck:
    name: str
    url: str


@dataclass
class ProductStatus:
    name: str
    url: str
    in_stock: Optional[bool]
    price: Optional[str]
    currency: Optional[str]
    availability_raw: Optional[str]


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


def load_config(path: str) -> List[ProductCheck]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    products = []
    for item in data.get("products", []):
        name = item.get("name") or item.get("url") or "Unknown"
        url = item.get("url")
        if not url:
            continue
        products.append(ProductCheck(name=name, url=url))
    return products


def fetch_html(url: str, timeout: int = 20) -> str:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def _extract_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            raw = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    items.append(entry)
        elif isinstance(raw, dict):
            items.append(raw)
    return items


def _parse_product_from_json_ld(items: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    for entry in items:
        if entry.get("@type") == "Product":
            offers = entry.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            availability = offers.get("availability")
            price = offers.get("price")
            currency = offers.get("priceCurrency")
            return availability, str(price) if price is not None else None, currency
    return None, None, None


def _find_variant_with_availability(data: Any) -> Optional[Dict[str, Any]]:
    if isinstance(data, dict):
        if "masterVariant" in data and isinstance(data["masterVariant"], dict):
            variant = data["masterVariant"]
            if "availability" in variant or "prices" in variant:
                return variant
        if all(key in data for key in ("availability", "prices", "sku")):
            return data
        for value in data.values():
            found = _find_variant_with_availability(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_variant_with_availability(item)
            if found:
                return found
    return None


def _find_sku_objects(data: Any, sku: str) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if data.get("sku") == sku:
            matches.append(data)
        for value in data.values():
            matches.extend(_find_sku_objects(value, sku))
    elif isinstance(data, list):
        for item in data:
            matches.extend(_find_sku_objects(item, sku))
    return matches


def _extract_from_inline_json(
    soup: BeautifulSoup, sku: Optional[str]
) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
    for script in soup.find_all("script"):
        if not script.string:
            continue
        text = script.string.strip()
        if not text or not (text.startswith("{") or text.startswith("[")):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if sku:
            candidates = _find_sku_objects(data, sku)
            if candidates:
                # Prefer objects with explicit price or stock fields.
                candidates.sort(
                    key=lambda item: (
                        "price" not in item and "prices" not in item,
                        "isOnStock" not in item and "availableQuantity" not in item,
                    )
                )
                sku_obj = candidates[0]
                price_value = None
                currency = None
                price = sku_obj.get("price")
                if isinstance(price, dict):
                    cent_amount = price.get("centAmount")
                    fraction_digits = price.get("fractionDigits", 2)
                    currency = price.get("currencyCode")
                    if cent_amount is not None and fraction_digits is not None:
                        price_value = (
                            f"{cent_amount / (10 ** fraction_digits):.{fraction_digits}f}"
                        )
                in_stock = None
                if "isOnStock" in sku_obj:
                    in_stock = bool(sku_obj.get("isOnStock"))
                return in_stock, price_value, currency
        variant = _find_variant_with_availability(data)
        if not variant:
            continue
        availability = variant.get("availability", {}).get("channels", {})
        if availability:
            in_stock = any(
                channel.get("isOnStock") and channel.get("availableQuantity", 0) > 0
                for channel in availability.values()
                if isinstance(channel, dict)
            )
        else:
            in_stock = None
        price_value = None
        currency = None
        prices = variant.get("prices") or []
        if isinstance(prices, list) and prices:
            price = None
            for entry in prices:
                if entry.get("country") == "US":
                    price = entry
                    break
            if not price:
                price = prices[0]
            value = price.get("value", {}) if isinstance(price, dict) else {}
            cent_amount = value.get("centAmount")
            fraction_digits = value.get("fractionDigits", 2)
            currency = value.get("currencyCode")
            if cent_amount is not None and fraction_digits is not None:
                price_value = f"{cent_amount / (10 ** fraction_digits):.{fraction_digits}f}"
        return in_stock, price_value, currency
    return None, None, None


def _extract_sku_from_url(url: str) -> Optional[str]:
    part = url.rstrip("/").split("/")[-1]
    return part if part else None


def parse_status(html: str, fallback_name: str, url: str) -> ProductStatus:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    meta_title = soup.find("meta", attrs={"property": "og:title"})
    if meta_title and meta_title.get("content"):
        title = meta_title["content"].strip()

    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)

    availability_raw = None
    price = None
    currency = None

    json_ld_items = _extract_json_ld(soup)
    availability_raw, price, currency = _parse_product_from_json_ld(json_ld_items)

    in_stock = None
    # If JSON-LD is missing, parse inline JSON payloads.
    if availability_raw is None and price is None:
        sku = _extract_sku_from_url(url)
        inline_in_stock, inline_price, inline_currency = _extract_from_inline_json(
            soup, sku
        )
        if inline_in_stock is not None:
            in_stock = inline_in_stock
        if inline_price is not None:
            price = inline_price
        if inline_currency is not None:
            currency = inline_currency
    # Prefer the buy button text for stock state when present.
    if in_stock is None:
        button = soup.find(
            "button",
            class_=lambda value: value and "btn-yellow" in value.split(),
        )
        if button:
            button_text = button.get_text(" ", strip=True).lower()
            if "out of stock" in button_text:
                in_stock = False
            elif "add to cart" in button_text or "add to bag" in button_text:
                in_stock = True
            elif "notify" in button_text:
                in_stock = False
        else:
            button_text = ""

    text = soup.get_text(" ", strip=True).lower()

    if availability_raw:
        if "instock" in availability_raw.lower():
            in_stock = True
        elif "outofstock" in availability_raw.lower():
            in_stock = False

    if in_stock is None:
        if "out of stock" in text:
            in_stock = False
        elif "add to cart" in text or "add to bag" in text:
            in_stock = True

    if price is None:
        # Known stable selector from Nikon PDP snapshots.
        price_node = soup.select_one('[data-testid="brow-product-price"]')
        if not price_node:
            price_node = soup.select_one('span[class^="ProductInformation_price__"]')
        if not price_node:
            price_node = soup.select_one('p[class^="ProductInfo_productPrice__"]')
        if price_node:
            price = price_node.get_text(strip=True) or None
        else:
            meta_price = soup.find("meta", attrs={"property": "product:price:amount"})
            if meta_price and meta_price.get("content"):
                price = meta_price["content"].strip()
            elif soup.find("meta", attrs={"property": "og:price:amount"}):
                price = soup.find("meta", attrs={"property": "og:price:amount"}).get(
                    "content"
                )

    if currency is None:
        meta_currency = soup.find("meta", attrs={"property": "product:price:currency"})
        if meta_currency and meta_currency.get("content"):
            currency = meta_currency["content"].strip()

    return ProductStatus(
        name=title or fallback_name,
        url=url,
        in_stock=in_stock,
        price=price,
        currency=currency,
        availability_raw=availability_raw,
    )


def load_state(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return {}


def save_state(path: str, state: Dict[str, Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def format_status_message(status: ProductStatus) -> str:
    availability = (
        "in stock"
        if status.in_stock is True
        else "out of stock"
        if status.in_stock is False
        else "unknown"
    )
    price = "unknown"
    if status.price:
        currency = f" {status.currency}" if status.currency else ""
        price = f"{status.price}{currency}"
    return (
        f"{status.name}\n"
        f"Availability: {availability}\n"
        f"Price: {price}\n"
        f"{status.url}"
    )


def send_discord(webhook_url: str, message: str) -> None:
    payload = {"content": message}
    response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()


def check_products(
    products: List[ProductCheck],
    webhook_url: Optional[str],
    state_path: str,
    notify_all: bool,
) -> None:
    state = load_state(state_path)
    new_state: Dict[str, Dict[str, Any]] = {}

    for product in products:
        html = fetch_html(product.url)
        status = parse_status(html, product.name, product.url)

        previous = state.get(product.url, {})
        new_state[product.url] = {
            "in_stock": status.in_stock,
            "price": status.price,
            "currency": status.currency,
        }

        should_notify = notify_all
        if not notify_all:
            if status.in_stock and previous.get("in_stock") is not True:
                should_notify = True
            elif (
                status.price
                and previous.get("price")
                and status.price != previous.get("price")
                and status.in_stock
            ):
                should_notify = True

        message = format_status_message(status)
        print(message)

        if should_notify and webhook_url:
            send_discord(webhook_url, message)

    save_state(state_path, new_state)


def run_loop(
    products: List[ProductCheck],
    webhook_url: Optional[str],
    state_path: str,
    notify_all: bool,
    interval_seconds: int,
) -> None:
    while True:
        check_products(products, webhook_url, state_path, notify_all)
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Nikon lens availability.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument(
        "--state-file",
        default=".state.json",
        help="Path to persisted state JSON.",
    )
    parser.add_argument(
        "--loop-minutes",
        type=int,
        default=0,
        help="If set, run continuously every N minutes.",
    )
    parser.add_argument(
        "--notify-all",
        action="store_true",
        help="Notify on every run instead of only on changes.",
    )
    args = parser.parse_args()

    webhook_url = "https://discord.com/api/webhooks/683341392830922753/Qmrw_RWvLaRxUsTC-vLcQ6PjYDD0A--qCvjCI7xA72VlFop9-vkZerUnUlycQiij4qm6"
    products = load_config(args.config)

    if not products:
        raise SystemExit("No products found in config.json.")

    if args.loop_minutes and args.loop_minutes > 0:
        run_loop(
            products,
            webhook_url,
            args.state_file,
            args.notify_all,
            args.loop_minutes * 60,
        )
    else:
        check_products(products, webhook_url, args.state_file, args.notify_all)


if __name__ == "__main__":
    main()

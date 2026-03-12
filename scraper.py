import asyncio
import os
import re
from dataclasses import dataclass, asdict
from typing import Callable
from playwright.async_api import async_playwright, Page

# On Railway/Docker use headless=True; locally keep headed to avoid bot detection
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"


PROPERTY_TYPE_IDS = {
    "hotel": "204",
    "apartment": "201",
    "hostel": "203",
    "villa": "213",
    "guest house": "216",
}

# Max km for each distance option — applied as post-scrape filter on
# the "X km from centre" text scraped from each card
DISTANCE_MAX_KM = {
    "less_1km": 1.0,
    "less_3km": 3.0,
    "less_5km": 5.0,
}

# Price selectors to try in order
PRICE_SELECTORS = [
    "[data-testid='price-and-discounted-price']",
    ".prco-valign-middle-helper",
    "[class*='price'] [class*='cost']",
]


@dataclass
class Hotel:
    name: str
    price_per_night: float | None
    total_price: float | None
    currency: str
    stars: int | None
    score: float | None
    property_type: str
    distance_from_centre: str
    breakfast_included: bool
    url: str



def build_search_url(
    ss: str,
    dest_id: str,
    dest_type: str,
    checkin: str,
    checkout: str,
    adults: int = 2,
    rooms: int = 1,
    stars_filter: list[int] | None = None,
    property_type_filter: list[str] | None = None,
    breakfast_filter: bool = False,
) -> str:
    base = "https://www.booking.com/searchresults.html"
    params = (
        f"?ss={ss.replace(' ', '+').replace(',', '%2C')}"
        f"&checkin={checkin}"
        f"&checkout={checkout}"
        f"&group_adults={adults}"
        f"&no_rooms={rooms}"
        f"&lang=en-gb"
    )
    if dest_id:
        params += f"&dest_id={dest_id}&dest_type={dest_type}&search_selected=true"

    nflt_parts: list[str] = []
    if stars_filter:
        for s in stars_filter:
            nflt_parts.append(f"class%3D{s}")
    if property_type_filter:
        for pt in property_type_filter:
            ht_id = PROPERTY_TYPE_IDS.get(pt.lower())
            if ht_id:
                nflt_parts.append(f"ht_id%3D{ht_id}")
    if breakfast_filter:
        nflt_parts.append("mealplan%3D1")
    if nflt_parts:
        params += "&nflt=" + "%3B".join(nflt_parts)

    return base + params


def parse_price(text: str) -> tuple[float | None, str]:
    if not text:
        return None, ""
    match = re.search(
        r"([€$£]|EUR|USD|GBP|SEK|NOK|DKK|kr)[\s\xa0]*([\d\s\xa0,.]+)"
        r"|"
        r"([\d\s\xa0,.]+)[\s\xa0]*([€$£]|EUR|USD|GBP|SEK|NOK|DKK|kr)",
        text,
    )
    if match:
        if match.group(1):
            currency, raw = match.group(1), match.group(2)
        else:
            currency, raw = match.group(4), match.group(3)
        raw = re.sub(r"[\s\xa0,]", "", raw)
        try:
            return float(raw), currency
        except ValueError:
            pass
    return None, ""


def parse_distance_km(text: str) -> float | None:
    """Parse '0.9 km from centre' → 0.9, '300 m from centre' → 0.3"""
    m = re.search(r"([\d.,]+)\s*(km|m)\s+from", text, re.IGNORECASE)
    if m:
        val = float(m.group(1).replace(",", "."))
        if m.group(2).lower() == "m":
            val /= 1000
        return val
    return None


async def scrape(
    city: str,
    checkin: str,
    checkout: str,
    adults: int = 2,
    rooms: int = 1,
    stars_filter: list[int] | None = None,
    property_type_filter: list[str] | None = None,
    distance_filter: str | None = None,
    breakfast_filter: bool = False,
    available_only: bool = True,
    max_results: int = 50,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    def progress(msg: str):
        if on_progress:
            on_progress(msg)

    max_km = DISTANCE_MAX_KM.get(distance_filter) if distance_filter else None

    url = build_search_url(
        ss=city,
        dest_id="",
        dest_type="city",
        checkin=checkin,
        checkout=checkout,
        adults=adults,
        rooms=rooms,
        stars_filter=stars_filter,
        property_type_filter=property_type_filter,
        breakfast_filter=breakfast_filter,
    )

    async with async_playwright() as p:
        progress("Launching browser…")
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--window-size=1440,900",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Remove webdriver fingerprint
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())

        progress(f"Navigating to Booking.com — {city}…")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Dismiss cookie banner
        for selector in ["[id*='onetrust-accept']", "button:has-text('Accept')"]:
            try:
                await page.click(selector, timeout=2500)
            except Exception:
                pass

        progress("Waiting for results…")
        try:
            await page.wait_for_selector("[data-testid='property-card']", timeout=15000)
        except Exception:
            progress("No results found or page timed out.")
            await browser.close()
            return []

        hotels = await _collect_results(page, max_results, max_km, available_only, progress)
        await browser.close()

    progress(f"Done — found {len(hotels)} properties.")
    return [asdict(h) for h in hotels]


async def _collect_results(
    page: Page,
    max_results: int,
    max_km: float | None,
    available_only: bool,
    progress: Callable[[str], None],
) -> list[Hotel]:
    hotels: list[Hotel] = []
    seen_names: set[str] = set()

    for _ in range(5):
        cards = await page.query_selector_all("[data-testid='property-card']")
        progress(f"Collecting results… ({len(hotels)}/{max_results} found so far, {len(cards)} cards on page)")

        for card in cards:
            try:
                # Skip sold-out listings (availability-cta-btn is on ALL cards — it's "See availability")
                if available_only:
                    unavail = await card.query_selector(
                        ".soldout_property, [data-testid='soldout-property']"
                    )
                    if unavail:
                        continue

                name_el = await card.query_selector("[data-testid='title']")
                name = (await name_el.inner_text()).strip() if name_el else "Unknown"
                if name in seen_names:
                    continue
                seen_names.add(name)

                # Distance — post-filter if requested
                dist_el = await card.query_selector("[data-testid='distance']")
                distance_text = (await dist_el.inner_text()).strip() if dist_el else "N/A"
                if max_km is not None:
                    km = parse_distance_km(distance_text)
                    if km is None or km > max_km:
                        continue

                # Price
                price_per_night, currency = None, ""
                for sel in PRICE_SELECTORS:
                    price_el = await card.query_selector(sel)
                    if price_el:
                        price_text = (await price_el.inner_text()).strip()
                        price_per_night, currency = parse_price(price_text)
                        if price_per_night:
                            break

                total_el = await card.query_selector("[data-testid='taxes-and-charges']")
                total_text = (await total_el.inner_text()).strip() if total_el else ""
                total_price = parse_price(total_text)[0] if total_text else None

                # Stars: aria-label on wrapper, e.g. "4 out of 5"
                stars = None
                stars_el = await card.query_selector("[data-testid='rating-stars']")
                if stars_el:
                    wrapper = await stars_el.evaluate_handle("el => el.closest('[aria-label]')")
                    aria = await wrapper.get_attribute("aria-label") if wrapper else None
                    if aria:
                        m = re.search(r"(\d+)\s+out\s+of", aria)
                        stars = int(m.group(1)) if m else None

                # Score
                score = None
                score_el = await card.query_selector("[data-testid='review-score']")
                if score_el:
                    score_text = (await score_el.inner_text()).strip()
                    m = re.search(r"\b(\d+[.,]\d+)\b", score_text)
                    if m:
                        try:
                            score = float(m.group(1).replace(",", "."))
                        except ValueError:
                            pass

                # Breakfast included
                breakfast_included = False
                try:
                    card_text = await card.inner_text()
                    if "breakfast included" in card_text.lower():
                        breakfast_included = True
                except Exception:
                    pass

                link_el = await card.query_selector("a[data-testid='title-link']")
                href = await link_el.get_attribute("href") if link_el else ""
                full_url = href if href.startswith("http") else f"https://www.booking.com{href}"

                hotels.append(Hotel(
                    name=name,
                    price_per_night=price_per_night,
                    total_price=total_price,
                    currency=currency,
                    stars=stars,
                    score=score,
                    property_type="N/A",
                    distance_from_centre=distance_text,
                    breakfast_included=breakfast_included,
                    url=full_url,
                ))

                if len(hotels) >= max_results:
                    return hotels

            except Exception as e:
                progress(f"Card error: {type(e).__name__}: {e}")
                continue

        # Load more / scroll
        try:
            load_more = await page.query_selector(
                "button:has-text('Load more results'), [data-testid='pagination-button']"
            )
            if load_more:
                progress("Loading next page…")
                await load_more.click()
                await page.wait_for_load_state("networkidle", timeout=8000)
            else:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
        except Exception:
            break

    return hotels

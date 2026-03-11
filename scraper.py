import asyncio
import re
from dataclasses import dataclass, asdict
from playwright.async_api import async_playwright, Page


PROPERTY_TYPES = {
    "hotel": "204",
    "apartment": "201",
    "hostel": "203",
    "villa": "213",
    "guest house": "216",
}

DISTANCE_LABELS = {
    "less_1km": "Less than 1 km from centre",
    "less_3km": "Less than 3 km from centre",
    "less_5km": "Less than 5 km from centre",
}


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
    url: str


def build_search_url(city: str, checkin: str, checkout: str, adults: int = 2, rooms: int = 1) -> str:
    base = "https://www.booking.com/searchresults.html"
    params = (
        f"?ss={city.replace(' ', '+')}"
        f"&checkin={checkin}"
        f"&checkout={checkout}"
        f"&group_adults={adults}"
        f"&no_rooms={rooms}"
        f"&lang=en-gb"
    )
    return base + params


def parse_price(text: str) -> tuple[float | None, str]:
    if not text:
        return None, ""
    match = re.search(r"([€$£]|EUR|USD|GBP|SEK|NOK|DKK)?\s*([\d\s,.]+)", text)
    if match:
        currency = match.group(1) or ""
        raw = match.group(2).replace(" ", "").replace(",", "")
        try:
            return float(raw), currency
        except ValueError:
            pass
    return None, ""


def parse_stars(card) -> int | None:
    # Try aria-label like "4 stars"
    for el in card.query_selector_all("[aria-label*='star']"):
        label = el.get_attribute("aria-label") or ""
        m = re.search(r"(\d)", label)
        if m:
            return int(m.group(1))
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
    max_results: int = 50,
) -> list[dict]:
    url = build_search_url(city, checkin, checkout, adults, rooms)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
        )
        page = await context.new_page()

        # Block images/fonts to speed up loading
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Dismiss cookie banner if present
        try:
            await page.click("[id*='onetrust-accept']", timeout=3000)
        except Exception:
            pass
        try:
            await page.click("button:has-text('Accept')", timeout=2000)
        except Exception:
            pass

        # Apply filters via sidebar
        await _apply_filters(page, stars_filter, property_type_filter, distance_filter)

        # Scroll and collect results
        hotels = await _collect_results(page, max_results)

        await browser.close()

    return [asdict(h) for h in hotels]


async def _apply_filters(page: Page, stars: list[int] | None, prop_types: list[str] | None, distance: str | None):
    await page.wait_for_selector("[data-testid='property-card']", timeout=15000)

    if stars:
        for s in stars:
            try:
                selector = f"[data-filters-item='class:class={s}'] input, input[id*='class={s}']"
                checkbox = await page.query_selector(selector)
                if checkbox:
                    await checkbox.check()
                    await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

    if prop_types:
        for pt in prop_types:
            ht_id = PROPERTY_TYPES.get(pt.lower())
            if not ht_id:
                continue
            try:
                selector = f"[data-filters-item='ht_id:ht_id={ht_id}'] input, input[id*='ht_id={ht_id}']"
                checkbox = await page.query_selector(selector)
                if checkbox:
                    await checkbox.check()
                    await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

    if distance:
        dist_map = {
            "less_1km": "1000",
            "less_3km": "3000",
            "less_5km": "5000",
        }
        dist_val = dist_map.get(distance)
        if dist_val:
            try:
                selector = f"[data-filters-item*='distance={dist_val}'] input, input[id*='distance={dist_val}']"
                checkbox = await page.query_selector(selector)
                if checkbox:
                    await checkbox.check()
                    await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass


async def _collect_results(page: Page, max_results: int) -> list[Hotel]:
    hotels: list[Hotel] = []
    seen_names: set[str] = set()

    for _ in range(5):  # up to 5 scroll/load iterations
        cards = await page.query_selector_all("[data-testid='property-card']")

        for card in cards:
            try:
                # Name
                name_el = await card.query_selector("[data-testid='title']")
                name = (await name_el.inner_text()).strip() if name_el else "Unknown"

                if name in seen_names:
                    continue
                seen_names.add(name)

                # Price per night
                price_el = await card.query_selector("[data-testid='price-and-discounted-price'], .prco-valign-middle-helper")
                price_text = (await price_el.inner_text()).strip() if price_el else ""
                price_per_night, currency = parse_price(price_text)

                # Total price (sometimes shown separately)
                total_el = await card.query_selector("[data-testid='taxes-and-charges']")
                total_text = (await total_el.inner_text()).strip() if total_el else ""
                total_price = parse_price(total_text)[0] if total_text else None

                # Stars
                stars_el = await card.query_selector("[data-testid='rating-stars'], [aria-label*='star']")
                stars = None
                if stars_el:
                    aria = await stars_el.get_attribute("aria-label") or ""
                    m = re.search(r"(\d)", aria)
                    stars = int(m.group(1)) if m else None

                # Review score
                score_el = await card.query_selector("[data-testid='review-score'] .a3b8729ab1")
                score = None
                if score_el:
                    try:
                        score = float((await score_el.inner_text()).strip().replace(",", "."))
                    except ValueError:
                        pass

                # Property type
                type_el = await card.query_selector("[data-testid='property-card--type']")
                prop_type = (await type_el.inner_text()).strip() if type_el else "N/A"

                # Distance from centre
                dist_el = await card.query_selector("[data-testid='distance']")
                distance = (await dist_el.inner_text()).strip() if dist_el else "N/A"

                # URL
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
                    property_type=prop_type,
                    distance_from_centre=distance,
                    url=full_url,
                ))

                if len(hotels) >= max_results:
                    return hotels

            except Exception:
                continue

        # Load more / scroll
        try:
            load_more = await page.query_selector("button:has-text('Load more results'), [data-testid='pagination-button']")
            if load_more:
                await load_more.click()
                await page.wait_for_load_state("networkidle", timeout=8000)
            else:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
        except Exception:
            break

    return hotels

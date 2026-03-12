import asyncio
import csv
import io
import json
import os
import queue
import threading
from datetime import date, timedelta

from flask import Flask, render_template, request, jsonify, Response
from scraper import scrape
import db

app = Flask(__name__)
db.init_db()

_last_results: list[dict] = []
_last_search_params: dict = {}


@app.route("/")
def index():
    today = date.today()
    checkin_default = (today + timedelta(days=7)).isoformat()
    checkout_default = (today + timedelta(days=10)).isoformat()
    return render_template("index.html", checkin=checkin_default, checkout=checkout_default)


@app.route("/search/stream")
def search_stream():
    """SSE endpoint — streams progress then final results."""
    city = request.args.get("city", "").strip()
    checkin = request.args.get("checkin", "")
    checkout = request.args.get("checkout", "")
    adults = int(request.args.get("adults", 2))
    rooms = int(request.args.get("rooms", 1))
    stars = [int(s) for s in request.args.getlist("stars")]
    property_types = request.args.getlist("property_types")
    distance = request.args.get("distance") or None
    breakfast = request.args.get("breakfast", "false").lower() == "true"
    available_only = request.args.get("available_only", "true").lower() == "true"
    max_results = min(int(request.args.get("max_results", 30)), 100)

    if not city or not checkin or not checkout:
        def err_gen():
            yield _sse("error", "City, check-in, and check-out are required.")
        return Response(err_gen(), mimetype="text/event-stream")

    q: queue.Queue = queue.Queue()

    def on_progress(msg: str):
        q.put(("progress", msg))

    def run_scrape():
        global _last_results, _last_search_params
        try:
            results = asyncio.run(scrape(
                city=city,
                checkin=checkin,
                checkout=checkout,
                adults=adults,
                rooms=rooms,
                stars_filter=stars or None,
                property_type_filter=property_types or None,
                distance_filter=distance,
                breakfast_filter=breakfast,
                available_only=available_only,
                max_results=max_results,
                on_progress=on_progress,
            ))

            # Fetch previous prices before saving new ones
            hotel_names = [r["name"] for r in results]
            prev_prices = db.get_previous_prices(city, checkin, checkout, hotel_names)

            # Save new results to history
            db.save_results(city, checkin, checkout, results)

            # Enrich results with price change info
            for r in results:
                prev = prev_prices.get(r["name"])
                if prev and prev["price"] is not None and r["price_per_night"] is not None:
                    diff = r["price_per_night"] - prev["price"]
                    r["price_change"] = round(diff, 2)
                    r["price_change_pct"] = round((diff / prev["price"]) * 100, 1)
                    r["prev_scraped_at"] = prev["scraped_at"]
                else:
                    r["price_change"] = None
                    r["price_change_pct"] = None
                    r["prev_scraped_at"] = None

            _last_results = results
            _last_search_params = {
                "city": city, "checkin": checkin, "checkout": checkout,
                "adults": adults, "rooms": rooms, "stars": stars,
                "property_types": property_types, "distance": distance,
                "breakfast": breakfast, "available_only": available_only,
                "max_results": max_results,
            }
            q.put(("results", results))
        except Exception as e:
            q.put(("error", str(e)))

    thread = threading.Thread(target=run_scrape, daemon=True)
    thread.start()

    def generate():
        while True:
            try:
                event, data = q.get(timeout=120)
            except queue.Empty:
                yield _sse("error", "Timed out waiting for scraper.")
                break

            if event == "progress":
                yield _sse("progress", data)
            elif event == "results":
                yield _sse("results", json.dumps(data))
                break
            elif event == "error":
                yield _sse("error", data)
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


@app.route("/export/csv")
def export_csv():
    if not _last_results:
        return "No results to export.", 400

    output = io.StringIO()
    fieldnames = ["name", "property_type", "stars", "score", "price_per_night",
                  "total_price", "currency", "breakfast_included", "distance_from_centre", "url"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_last_results)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=booking_results.csv"},
    )


# ── Saved searches ──────────────────────────────────────────────────────────

@app.route("/saved-searches", methods=["GET"])
def list_saved_searches():
    return jsonify(db.list_saved_searches())


@app.route("/saved-searches", methods=["POST"])
def create_saved_search():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    params = data.get("params", {})
    if not name:
        return jsonify({"error": "Name is required"}), 400
    search_id = db.create_saved_search(name, params)
    return jsonify({"id": search_id, "name": name}), 201


@app.route("/saved-searches/<int:search_id>", methods=["DELETE"])
def delete_saved_search(search_id):
    db.delete_saved_search(search_id)
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, port=port)

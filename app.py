import asyncio
import csv
import io
import json
from datetime import date, timedelta

from flask import Flask, render_template, request, jsonify, Response
from scraper import scrape

app = Flask(__name__)

# Store last results in memory for CSV export
_last_results: list[dict] = []


@app.route("/")
def index():
    today = date.today()
    checkin_default = (today + timedelta(days=7)).isoformat()
    checkout_default = (today + timedelta(days=10)).isoformat()
    return render_template("index.html", checkin=checkin_default, checkout=checkout_default)


@app.route("/search", methods=["POST"])
def search():
    global _last_results

    data = request.get_json()

    city = data.get("city", "").strip()
    checkin = data.get("checkin", "")
    checkout = data.get("checkout", "")
    adults = int(data.get("adults", 2))
    rooms = int(data.get("rooms", 1))
    stars = [int(s) for s in data.get("stars", [])]
    property_types = data.get("property_types", [])
    distance = data.get("distance", None) or None
    max_results = min(int(data.get("max_results", 30)), 100)

    if not city or not checkin or not checkout:
        return jsonify({"error": "City, check-in, and check-out are required."}), 400

    try:
        results = asyncio.run(
            scrape(
                city=city,
                checkin=checkin,
                checkout=checkout,
                adults=adults,
                rooms=rooms,
                stars_filter=stars or None,
                property_type_filter=property_types or None,
                distance_filter=distance,
                max_results=max_results,
            )
        )
        _last_results = results
        return jsonify({"results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/export/csv")
def export_csv():
    if not _last_results:
        return "No results to export.", 400

    output = io.StringIO()
    fieldnames = ["name", "property_type", "stars", "score", "price_per_night", "total_price", "currency", "distance_from_centre", "url"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_last_results)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=booking_results.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)

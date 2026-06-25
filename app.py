from flask import Flask, jsonify, request, render_template
import sqlite3
import os
from rapidfuzz import fuzz, process

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "pos.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_product_query(extra_where="", extra_join="", params=(), enabled="all"):
    enabled_clause = ""
    if str(enabled) == "1":
        enabled_clause = "p.IsEnabled = 1"
    elif str(enabled) == "0":
        enabled_clause = "p.IsEnabled = 0"

    where_parts = [c for c in [enabled_clause, extra_where] if c]
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT
            p.Id,
            p.Name,
            p.Code,
            p.PLU,
            p.Price,
            p.Cost,
            p.Markup,
            p.IsTaxInclusivePrice,
            p.MeasurementUnit,
            p.IsEnabled,
            p.Description,
            p.LastPurchasePrice,
            pg.Name  AS GroupName,
            COALESCE(s.Quantity, 0) AS Stock,
            COALESCE(t.Name, '')   AS TaxName,
            COALESCE(t.Rate, 0)    AS TaxRate,
            COALESCE(t.IsFixed, 0) AS TaxIsFixed,
            GROUP_CONCAT(DISTINCT b.Value) AS Barcodes
        FROM Product p
        LEFT JOIN ProductGroup  pg ON p.ProductGroupId = pg.Id
        LEFT JOIN Stock          s ON p.Id = s.ProductId
        LEFT JOIN ProductTax    pt ON p.Id = pt.ProductId
        LEFT JOIN Tax            t ON pt.TaxId = t.Id
        LEFT JOIN Barcode         b ON p.Id = b.ProductId
        {extra_join}
        {where_clause}
        GROUP BY p.Id
        ORDER BY p.Name COLLATE NOCASE
    """
    return sql, params


def row_to_dict(row):
    d = dict(row)
    barcodes_raw = d.get("Barcodes") or ""
    d["Barcodes"] = [b for b in barcodes_raw.split(",") if b] if barcodes_raw else []
    # Compute display prices
    price = d["Price"] or 0.0
    cost = d["Cost"] or 0.0
    tax_rate = d["TaxRate"] or 0.0
    tax_is_fixed = bool(d["TaxIsFixed"])
    inclusive = bool(d["IsTaxInclusivePrice"])

    if tax_is_fixed:
        if inclusive:
            d["PriceSinIVA"] = round(price - tax_rate, 4)
            d["PriceConIVA"] = round(price, 4)
        else:
            d["PriceSinIVA"] = round(price, 4)
            d["PriceConIVA"] = round(price + tax_rate, 4)
    else:
        if inclusive:
            d["PriceSinIVA"] = round(price / (1 + tax_rate / 100), 4) if tax_rate else price
            d["PriceConIVA"] = round(price, 4)
        else:
            d["PriceSinIVA"] = round(price, 4)
            d["PriceConIVA"] = round(price * (1 + tax_rate / 100), 4) if tax_rate else price

    d["Margin"] = round((price - cost) / price * 100, 2) if price else 0.0
    return d


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "name")  # "name" | "barcode" | "code"
    enabled = request.args.get("enabled", "all")  # "all" | "1" | "0"
    limit = min(int(request.args.get("limit", 80)), 200)

    if not query:
        return jsonify({"results": [], "total": 0})

    conn = get_conn()
    try:
        if mode == "barcode":
            sql, params = build_product_query(
                extra_where="b.Value LIKE ?",
                params=(f"%{query}%",),
                enabled=enabled,
            )
            rows = conn.execute(sql, params).fetchall()
            results = [row_to_dict(r) for r in rows][:limit]
            return jsonify({"results": results, "total": len(results)})

        if mode == "code":
            sql, params = build_product_query(
                extra_where="p.Code LIKE ?",
                params=(f"{query}%",),
                enabled=enabled,
            )
            rows = conn.execute(sql, params).fetchall()
            results = [row_to_dict(r) for r in rows][:limit]
            return jsonify({"results": results, "total": len(results)})

        # Fuzzy name search:
        # 1. Pull candidate rows using LIKE on each token (fast pre-filter)
        tokens = query.split()
        like_clauses = " AND ".join(["p.Name LIKE ?" for _ in tokens])
        like_params = tuple(f"%{t}%" for t in tokens)

        sql_like, _ = build_product_query(extra_where=like_clauses, params=like_params, enabled=enabled)
        candidates = conn.execute(sql_like, like_params).fetchall()

        if not candidates:
            # Fall back to full fuzzy scan if no LIKE hits
            sql_all, _ = build_product_query(enabled=enabled)
            candidates = conn.execute(sql_all).fetchall()

        # 2. Score with rapidfuzz
        scored = []
        for row in candidates:
            name = row["Name"] or ""
            score = fuzz.WRatio(query.lower(), name.lower())
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [row_to_dict(r) for score, r in scored if score >= 45][:limit]
        return jsonify({"results": results, "total": len(results)})

    finally:
        conn.close()


@app.route("/api/product/<int:product_id>")
def product_detail(product_id):
    conn = get_conn()
    try:
        sql, _ = build_product_query(extra_where="p.Id = ?", params=(product_id,), enabled="all")
        row = conn.execute(sql, (product_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(row_to_dict(row))
    finally:
        conn.close()


@app.route("/api/groups")
def groups():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT Id, Name FROM ProductGroup ORDER BY Name COLLATE NOCASE"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/stats")
def stats():
    conn = get_conn()
    try:
        active = conn.execute("SELECT COUNT(*) FROM Product WHERE IsEnabled=1").fetchone()[0]
        inactive = conn.execute("SELECT COUNT(*) FROM Product WHERE IsEnabled=0").fetchone()[0]
        groups = conn.execute("SELECT COUNT(DISTINCT ProductGroupId) FROM Product WHERE IsEnabled=1").fetchone()[0]
        with_stock = conn.execute(
            "SELECT COUNT(*) FROM Product p JOIN Stock s ON p.Id=s.ProductId WHERE p.IsEnabled=1 AND s.Quantity>0"
        ).fetchone()[0]
        return jsonify({"active": active, "inactive": inactive, "total": active + inactive, "groups": groups, "withStock": with_stock})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=5000)

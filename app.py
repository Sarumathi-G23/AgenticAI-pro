from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, date, timedelta

app = Flask(__name__)
app.secret_key = "super-secret-key"  # for flash messages

DB_PATH = "inventory.db"


# ---------------- DB HELPERS ---------------- #

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # Products master
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        cost_price REAL DEFAULT 0,
        selling_price REAL DEFAULT 0,
        min_stock INTEGER DEFAULT 0,
        max_stock INTEGER DEFAULT 100,
        lead_time_days INTEGER DEFAULT 3,
        active INTEGER DEFAULT 1
    );
    """)

    # Current stock
    cur.execute("""
    CREATE TABLE IF NOT EXISTS current_stock (
        product_id INTEGER PRIMARY KEY,
        qty_in_hand INTEGER DEFAULT 0,
        last_updated TEXT,
        FOREIGN KEY(product_id) REFERENCES products(product_id)
    );
    """)

    # Weekly sales
    cur.execute("""
    CREATE TABLE IF NOT EXISTS weekly_sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        week_start_date TEXT,
        qty_sold INTEGER,
        FOREIGN KEY(product_id) REFERENCES products(product_id)
    );
    """)

    # Purchase orders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS purchase_orders (
        po_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT,
        week_start_date TEXT,
        status TEXT
    );
    """)

    # Purchase order items
    cur.execute("""
    CREATE TABLE IF NOT EXISTS purchase_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_id INTEGER,
        product_id INTEGER,
        product_name TEXT,
        qty INTEGER,
        FOREIGN KEY(po_id) REFERENCES purchase_orders(po_id),
        FOREIGN KEY(product_id) REFERENCES products(product_id)
    );
    """)

    conn.commit()
    conn.close()


# ---------- INITIALIZE DB AT STARTUP (Flask 3) ---------- #

init_db()  # safe to call many times; only creates tables if missing


# ---------------- AGENT CLASSES ---------------- #

class DataAgent:
    """
    DataAgent:
      - Reads from SQLite
      - Returns a 'state' dict with products, stock, sales
    """
    def collect_state(self):
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM products WHERE active = 1 ORDER BY product_id;")
        products = rows_to_dicts(cur.fetchall())

        cur.execute("""
            SELECT p.product_id, p.name,
                   COALESCE(s.qty_in_hand, 0) AS qty_in_hand,
                   s.last_updated
            FROM products p
            LEFT JOIN current_stock s ON p.product_id = s.product_id
            WHERE p.active = 1
            ORDER BY p.product_id;
        """)
        stock = rows_to_dicts(cur.fetchall())

        cur.execute("""
            SELECT * FROM weekly_sales
            ORDER BY date(week_start_date) DESC;
        """)
        sales = rows_to_dicts(cur.fetchall())

        conn.close()

        sales_by_product = {}
        for row in sales:
            pid = row["product_id"]
            sales_by_product.setdefault(pid, []).append(row)

        stock_by_product = {row["product_id"]: row for row in stock}

        return {
            "products": products,
            "stock_by_product": stock_by_product,
            "sales_by_product": sales_by_product,
        }


class ForecastAgent:
    """
    ForecastAgent:
      - Predicts next week demand = average of last N weeks (default 4)
      - Simple model, but can be replaced by ML/transformer later
    """
    def __init__(self, max_weeks: int = 4):
        self.max_weeks = max_weeks

    def _avg_sales(self, rows):
        if not rows:
            return 0.0
        considered = rows[: self.max_weeks]
        qtys = [int(r["qty_sold"]) for r in considered]
        return sum(qtys) / len(qtys)

    def forecast(self, state):
        forecasts = {}
        for prod in state["products"]:
            pid = prod["product_id"]
            sales_rows = state["sales_by_product"].get(pid, [])
            forecasts[pid] = self._avg_sales(sales_rows)
        return forecasts


class ReplenishmentAgent:
    """
    ReplenishmentAgent:
      - Uses forecast + min/max stock
      - Decides suggested weekly order for each product
    """
    def __init__(self, safety_factor: float = 1.5):
        self.safety_factor = safety_factor

    def build_plan(self, state, forecasts):
        plan = []
        for prod in state["products"]:
            pid = prod["product_id"]
            name = prod["name"]
            min_stock = int(prod.get("min_stock", 0))
            max_stock = int(prod.get("max_stock", 100))

            stock_row = state["stock_by_product"].get(pid)
            current_stock = int(stock_row["qty_in_hand"]) if stock_row else 0

            avg_sales = float(forecasts.get(pid, 0.0))
            forecast_next_week = avg_sales

            required_stock = max(min_stock, forecast_next_week * self.safety_factor)
            if required_stock > max_stock:
                required_stock = max_stock

            order_qty = int(round(required_stock - current_stock))
            if order_qty < 0:
                order_qty = 0

            if avg_sales < 1 and current_stock > 0:
                reason = "Slow-moving item (avg < 1/week) with stock available – no order."
                order_qty = 0
            else:
                reason = (
                    f"Avg sales ≈ {avg_sales:.1f}/week, "
                    f"current stock = {current_stock}, "
                    f"target stock ≈ {required_stock:.1f}."
                )

            plan.append({
                "product_id": pid,
                "name": name,
                "avg_weekly_sales": round(avg_sales, 2),
                "current_stock": current_stock,
                "forecast_next_week": round(forecast_next_week, 2),
                "suggested_order_qty": order_qty,
                "reason": reason,
            })
        return plan


class BudgetAgent:
    """
    BudgetAgent:
      - Makes sure total weekly order cost is within budget
      - Drops low-selling items first if budget crossed
    """
    def __init__(self, weekly_budget: float = 25000.0):
        self.weekly_budget = weekly_budget

    def apply(self, plan, products):
        price_index = {p["product_id"]: float(p.get("cost_price", 0.0)) for p in products}

        for row in plan:
            pid = row["product_id"]
            cp = price_index.get(pid, 0.0)
            row["unit_cost"] = cp
            row["line_cost"] = cp * row["suggested_order_qty"]

        total_cost = sum(r["line_cost"] for r in plan)
        if total_cost <= self.weekly_budget:
            for r in plan:
                r["budget_note"] = "Within weekly budget"
            return plan

        # Sort by avg_weekly_sales ascending -> low importance drop first
        sorted_plan = sorted(plan, key=lambda r: r["avg_weekly_sales"])
        excess = total_cost - self.weekly_budget

        for row in sorted_plan:
            if excess <= 0:
                break
            if row["suggested_order_qty"] <= 0:
                continue
            # Drop this item from order
            reduction_cost = row["line_cost"]
            row["suggested_order_qty"] = 0
            row["line_cost"] = 0
            row["budget_note"] = "Dropped due to budget limit"
            excess -= reduction_cost

        for r in sorted_plan:
            if "budget_note" not in r:
                r["budget_note"] = "Kept in final order"

        return sorted_plan


class ReportingAgent:
    """
    ReportingAgent:
      - Creates high-level summary text for the weekly plan
    """
    def summarize(self, plan):
        if not plan:
            return "No products found in the system."

        total_products = len(plan)
        total_to_order = sum(p["suggested_order_qty"] for p in plan)
        slow_moving = sum(
            1 for p in plan if p["avg_weekly_sales"] < 1 and p["current_stock"] > 0
        )
        zero_stock_items = sum(1 for p in plan if p["current_stock"] == 0)
        no_order_this_week = sum(1 for p in plan if p["suggested_order_qty"] == 0)

        summary_lines = [
            f"Analyzed {total_products} products for this week's replenishment.",
            f"Total quantity suggested to order: {total_to_order} units.",
            f"{slow_moving} items are slow-moving and are skipped for ordering.",
            f"{zero_stock_items} items currently have zero stock and are prioritized where needed.",
            f"For {no_order_this_week} items, no purchase is needed this week."
        ]
        return " ".join(summary_lines)


# --------- Instantiate agents --------- #

data_agent = DataAgent()
forecast_agent = ForecastAgent(max_weeks=4)
replenish_agent = ReplenishmentAgent(safety_factor=1.5)
budget_agent = BudgetAgent(weekly_budget=25000.0)  # adjust if you want
reporting_agent = ReportingAgent()


# ---------------- ROUTES ---------------- #

@app.route("/")
def home():
    return redirect(url_for("planner"))


# ---- Products ---- #

@app.route("/products", methods=["GET", "POST"])
def products():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        min_stock = int(request.form.get("min_stock", "0") or 0)
        max_stock = int(request.form.get("max_stock", "0") or 0)
        cost_price = float(request.form.get("cost_price", "0") or 0)
        selling_price = float(request.form.get("selling_price", "0") or 0)
        lead_time_days = int(request.form.get("lead_time_days", "3") or 3)

        if not name:
            flash("Product name is required", "danger")
        else:
            cur.execute("""
                INSERT INTO products (name, category, cost_price, selling_price, min_stock, max_stock, lead_time_days)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, category, cost_price, selling_price, min_stock, max_stock, lead_time_days))
            conn.commit()
            flash("Product added", "success")

        return redirect(url_for("products"))

    cur.execute("SELECT * FROM products WHERE active = 1 ORDER BY product_id;")
    products = rows_to_dicts(cur.fetchall())
    conn.close()

    return render_template("products.html", products=products)


# ---- Stock ---- #

@app.route("/stock", methods=["GET", "POST"])
def stock():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        now_str = datetime.now().isoformat(timespec="seconds")
        cur.execute("SELECT product_id FROM products WHERE active = 1 ORDER BY product_id;")
        pids = [row["product_id"] for row in cur.fetchall()]
        for pid in pids:
            field = f"qty_{pid}"
            qty_str = request.form.get(field, "0") or "0"
            qty = int(qty_str)
            cur.execute("""
                INSERT INTO current_stock (product_id, qty_in_hand, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    qty_in_hand = excluded.qty_in_hand,
                    last_updated = excluded.last_updated;
            """, (pid, qty, now_str))
        conn.commit()
        flash("Stock updated", "success")
        return redirect(url_for("stock"))

    # GET
    cur.execute("""
        SELECT p.product_id, p.name,
               COALESCE(s.qty_in_hand, 0) AS qty_in_hand
        FROM products p
        LEFT JOIN current_stock s ON p.product_id = s.product_id
        WHERE p.active = 1
        ORDER BY p.product_id;
    """)
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return render_template("stock.html", rows=rows)


# ---- Sales ---- #

@app.route("/sales", methods=["GET", "POST"])
def sales():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        product_id = int(request.form.get("product_id"))
        week_start_date = request.form.get("week_start_date")
        qty_sold = int(request.form.get("qty_sold", "0") or 0)

        cur.execute("""
            INSERT INTO weekly_sales (product_id, week_start_date, qty_sold)
            VALUES (?, ?, ?);
        """, (product_id, week_start_date, qty_sold))
        conn.commit()
        flash("Sales row added", "success")
        return redirect(url_for("sales"))

    cur.execute("SELECT * FROM products WHERE active = 1 ORDER BY product_id;")
    products = rows_to_dicts(cur.fetchall())

    cur.execute("""
        SELECT w.id, w.week_start_date, w.qty_sold, p.name
        FROM weekly_sales w
        JOIN products p ON p.product_id = w.product_id
        ORDER BY date(w.week_start_date) DESC
        LIMIT 10;
    """)
    latest_sales = rows_to_dicts(cur.fetchall())

    conn.close()

    # Default date = last Monday
    today = date.today()
    diff_to_monday = (today.weekday())  # 0 = Monday
    monday = today - timedelta(days=diff_to_monday)
    default_date = monday.isoformat()

    return render_template(
        "sales.html",
        products=products,
        latest_sales=latest_sales,
        default_date=default_date,
    )


# ---- Planner + Auto Order ---- #

def run_agent_pipeline():
    """
    This is the core Agentic AI pipeline:
    DataAgent -> ForecastAgent -> ReplenishmentAgent -> BudgetAgent -> ReportingAgent
    """
    state = data_agent.collect_state()
    forecasts = forecast_agent.forecast(state)
    raw_plan = replenish_agent.build_plan(state, forecasts)
    budgeted_plan = budget_agent.apply(raw_plan, state["products"])
    summary = reporting_agent.summarize(budgeted_plan)
    return budgeted_plan, summary


@app.route("/planner")
def planner():
    plan, summary = run_agent_pipeline()
    return render_template("planner.html", plan=plan, summary=summary)


@app.route("/auto_order", methods=["POST"])
def auto_order():
    plan, summary = run_agent_pipeline()
    items_to_order = [p for p in plan if p["suggested_order_qty"] > 0]

    if not items_to_order:
        flash("No items require ordering this week", "warning")
        return redirect(url_for("planner"))

    conn = get_connection()
    cur = conn.cursor()

    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    now_str = datetime.now().isoformat(timespec="seconds")

    cur.execute("""
        INSERT INTO purchase_orders (created_at, week_start_date, status)
        VALUES (?, ?, ?);
    """, (now_str, week_start.isoformat(), "CREATED"))
    po_id = cur.lastrowid

    for item in items_to_order:
        cur.execute("""
            INSERT INTO purchase_order_items (po_id, product_id, product_name, qty)
            VALUES (?, ?, ?, ?);
        """, (po_id, item["product_id"], item["name"], item["suggested_order_qty"]))

    conn.commit()

    cur.execute("""
        SELECT product_id, product_name, qty
        FROM purchase_order_items
        WHERE po_id = ?;
    """, (po_id,))
    po_items = rows_to_dicts(cur.fetchall())
    conn.close()

    flash(f"Purchase order #{po_id} created for week starting {week_start}", "success")
    # Show same planner page but with PO data
    return render_template("planner.html", plan=plan, summary=summary, po_id=po_id, po_items=po_items, po_week=week_start)


if __name__ == "__main__":
    app.run(debug=True)

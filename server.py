from flask import Flask, render_template, request, jsonify
import sqlite3, json, os, base64, threading
from datetime import date, datetime, timedelta
from pathlib import Path

app = Flask(__name__)
BASE = Path(__file__).parent
DB  = BASE / "nutrition.db"

ANTHROPIC_KEY = ""  # set via /api/settings

# ── DB ────────────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(
        id INTEGER PRIMARY KEY DEFAULT 1,
        api_key TEXT DEFAULT '',
        cal_min REAL DEFAULT 1400, cal_max REAL DEFAULT 1900,
        prot_min REAL DEFAULT 150,  prot_max REAL DEFAULT 220,
        fat_min REAL DEFAULT 40,    fat_max REAL DEFAULT 70,
        carb_min REAL DEFAULT 100,  carb_max REAL DEFAULT 180
    );
    INSERT OR IGNORE INTO settings(id) VALUES(1);

    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, category TEXT NOT NULL,
        cal REAL NOT NULL, prot REAL NOT NULL,
        fat REAL NOT NULL, carb REAL NOT NULL, fiber REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS recipes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, units INTEGER DEFAULT 1,
        total_weight REAL, total_cal REAL, total_prot REAL,
        total_fat REAL, total_carb REAL, notes TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS recipe_ing(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
        product_id INTEGER, product_name TEXT,
        grams REAL, cal REAL, prot REAL, fat REAL, carb REAL
    );

    CREATE TABLE IF NOT EXISTS daily_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date TEXT NOT NULL, meal TEXT NOT NULL,
        name TEXT NOT NULL, grams REAL,
        cal REAL, prot REAL, fat REAL, carb REAL,
        src TEXT DEFAULT 'manual',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS weight_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date TEXT NOT NULL UNIQUE,
        kg REAL NOT NULL, notes TEXT DEFAULT ''
    );
    """)
    c.commit(); c.close()

init()

# ── Helpers ───────────────────────────────────────────────────────────────────
def rows(sql, params=()):
    c = db(); r = [dict(x) for x in c.execute(sql, params).fetchall()]; c.close(); return r

def run(sql, params=()):
    c = db(); cur = c.execute(sql, params); c.commit(); lid = cur.lastrowid; c.close(); return lid

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

# Settings
@app.route("/api/settings")
def get_settings():
    return jsonify(rows("SELECT * FROM settings WHERE id=1")[0])

@app.route("/api/settings", methods=["POST"])
def save_settings():
    d = request.json
    global ANTHROPIC_KEY
    ANTHROPIC_KEY = d.get("api_key","")
    run("UPDATE settings SET api_key=?,cal_min=?,cal_max=?,prot_min=?,prot_max=?,fat_min=?,fat_max=?,carb_min=?,carb_max=? WHERE id=1",
        (d["api_key"],d["cal_min"],d["cal_max"],d["prot_min"],d["prot_max"],d["fat_min"],d["fat_max"],d["carb_min"],d["carb_max"]))
    return jsonify({"ok":True})

# Products
@app.route("/api/products")
def get_products():
    cat = request.args.get("cat"); q = request.args.get("q")
    sql = "SELECT * FROM products WHERE 1=1"
    p = []
    if cat: sql += " AND category=?"; p.append(cat)
    if q:   sql += " AND name LIKE ?"; p.append(f"%{q}%")
    return jsonify(rows(sql + " ORDER BY name", p))

@app.route("/api/products", methods=["POST"])
def add_product():
    d = request.json
    pid = run("INSERT INTO products(name,category,cal,prot,fat,carb,fiber) VALUES(?,?,?,?,?,?,?)",
        (d["name"],d["category"],d["cal"],d["prot"],d["fat"],d["carb"],d.get("fiber",0)))
    return jsonify({"id":pid,"ok":True})

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def del_product(pid):
    run("DELETE FROM products WHERE id=?", (pid,)); return jsonify({"ok":True})

# Scan
@app.route("/api/scan", methods=["POST"])
def scan():
    key = rows("SELECT api_key FROM settings WHERE id=1")[0]["api_key"]
    if not key: return jsonify({"error":"חסר API Key — הגדר בהגדרות ⚙️"}), 400
    file = request.files.get("image")
    if not file: return jsonify({"error":"לא נשלחה תמונה"}), 400
    try:
        import anthropic
        img = base64.standard_b64encode(file.read()).decode()
        mt = file.content_type or "image/jpeg"
        if mt in ("image/heic", "image/heif"):
            mt = "image/jpeg"
        cl = anthropic.Anthropic(api_key=key)
        msg = cl.messages.create(model="claude-opus-4-5", max_tokens=512,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":mt,"data":img}},
                {"type":"text","text":"""Extract from this nutrition label (per 100g).
Return ONLY JSON, no markdown:
{"name":"...","cal":0,"prot":0,"fat":0,"carb":0,"category":"dairy|meat|poultry|grains|vegetables|fruits|snacks|beverages|supplements|other"}"""}
            ]}])
        text = msg.content[0].text.strip().strip("```").lstrip("json").strip()
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Daily log
@app.route("/api/log")
def get_log():
    d = request.args.get("date", str(date.today()))
    s = rows("SELECT * FROM settings WHERE id=1")[0]
    entries = rows("SELECT * FROM daily_log WHERE log_date=? ORDER BY meal,created_at", (d,))
    meals = {"בוקר":[],"צהריים":[],"בניים":[],"ערב":[],"נוסף":[]}
    for e in entries:
        m = e["meal"] if e["meal"] in meals else "נוסף"
        meals[m].append(e)
    totals = {k: round(sum(e[k] for e in entries),1) for k in ["cal","prot","fat","carb"]}
    return jsonify({"meals":meals,"totals":totals,"settings":s,"date":d})

@app.route("/api/log", methods=["POST"])
def add_log():
    d = request.json
    run("INSERT INTO daily_log(log_date,meal,name,grams,cal,prot,fat,carb,src) VALUES(?,?,?,?,?,?,?,?,?)",
        (d.get("date",str(date.today())),d["meal"],d["name"],d["grams"],d["cal"],d["prot"],d["fat"],d["carb"],d.get("src","manual")))
    return jsonify({"ok":True})

@app.route("/api/log/<int:eid>", methods=["DELETE"])
def del_log(eid):
    run("DELETE FROM daily_log WHERE id=?", (eid,)); return jsonify({"ok":True})

# Recipes
@app.route("/api/recipes")
def get_recipes():
    recs = rows("SELECT * FROM recipes ORDER BY name")
    for r in recs:
        r["ingredients"] = rows("SELECT * FROM recipe_ing WHERE recipe_id=?", (r["id"],))
    return jsonify(recs)

@app.route("/api/recipes", methods=["POST"])
def add_recipe():
    d = request.json; ings = d.get("ingredients",[])
    tc = sum(i["cal"] for i in ings); tp = sum(i["prot"] for i in ings)
    tf = sum(i["fat"] for i in ings); tcb = sum(i["carb"] for i in ings)
    tw = sum(i["grams"] for i in ings)
    rid = run("INSERT INTO recipes(name,units,total_weight,total_cal,total_prot,total_fat,total_carb,notes) VALUES(?,?,?,?,?,?,?,?)",
        (d["name"],d.get("units",1),tw,tc,tp,tf,tcb,d.get("notes","")))
    for i in ings:
        run("INSERT INTO recipe_ing(recipe_id,product_id,product_name,grams,cal,prot,fat,carb) VALUES(?,?,?,?,?,?,?,?)",
            (rid,i.get("product_id"),i["product_name"],i["grams"],i["cal"],i["prot"],i["fat"],i["carb"]))
    return jsonify({"id":rid,"ok":True})

@app.route("/api/recipes/<int:rid>", methods=["DELETE"])
def del_recipe(rid):
    run("DELETE FROM recipes WHERE id=?", (rid,)); return jsonify({"ok":True})

# Weight
@app.route("/api/weight")
def get_weight():
    data = rows("SELECT * FROM weight_log ORDER BY log_date ASC")
    for i,r in enumerate(data):
        if i==0: r["chg_kg"]=None; r["chg_pct"]=None
        else:
            prev=data[i-1]["kg"]; curr=r["kg"]
            r["chg_kg"]=round(curr-prev,2)
            r["chg_pct"]=round((curr-prev)/prev*100,2)
    return jsonify(data)

@app.route("/api/weight", methods=["POST"])
def add_weight():
    d = request.json
    run("INSERT OR REPLACE INTO weight_log(log_date,kg,notes) VALUES(?,?,?)",
        (d.get("date",str(date.today())),d["kg"],d.get("notes","")))
    return jsonify({"ok":True})

@app.route("/api/weight/<ld>", methods=["DELETE"])
def del_weight(ld):
    run("DELETE FROM weight_log WHERE log_date=?", (ld,)); return jsonify({"ok":True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)

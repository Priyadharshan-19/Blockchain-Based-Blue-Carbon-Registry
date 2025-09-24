from flask import Flask, request, jsonify
import sqlite3
import time
import hashlib
import json
import os
from threading import Timer, Lock
from web3 import Web3

app = Flask(__name__)
# allow both /verifier and /verifier/
app.url_map.strict_slashes = False

# register the verifier dashboard (verifier.py must be alongside this file)
from verifier import verifier_bp

DB_FILE = "mrv_data.db"

# ------------------ Blockchain Config (Ganache Desktop) ------------------
RPC_URL = "http://127.0.0.1:7545"  # <-- Replace with your Ganache RPC URL if different
REGISTRY_ADDRESS_RAW = "REPLACE_WITH_DEPLOYED_CONTRACT_ADDRESS"  # <-- Fill in your deployed contract address

# Deployer (Ganache account)
DEPLOYER_ADDRESS_RAW = "REPLACE_WITH_GANACHE_ACCOUNT_ADDRESS"    # <-- Your Ganache account address
DEPLOYER_PRIVATE_KEY = "REPLACE_WITH_GANACHE_PRIVATE_KEY"        # <-- Your Ganache private key (keep secret!)

CHAIN_ID = 1337  # Ganache default (adjust if different)

CONTRACT_ABI = [
    {
        "inputs": [
            {"internalType": "string", "name": "batchHash", "type": "string"},
            {"internalType": "string", "name": "areaId", "type": "string"},
            {"internalType": "uint256", "name": "timestamp", "type": "uint256"},
        ],
        "name": "commitHash",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ------------------ Web3 ------------------
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
if not w3.is_connected():
    raise RuntimeError(f"Cannot connect to RPC at {RPC_URL}. Start Ganache on this port.")
REGISTRY_ADDRESS = Web3.to_checksum_address(REGISTRY_ADDRESS_RAW)
DEPLOYER_ADDRESS = Web3.to_checksum_address(DEPLOYER_ADDRESS_RAW)
registry = w3.eth.contract(address=REGISTRY_ADDRESS, abi=CONTRACT_ABI)

# ------------------ Database ------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id TEXT,
            temperature REAL,
            humidity REAL,
            soil_moisture REAL,
            co2_proxy REAL,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            committed_at TEXT,
            count INTEGER,
            batch_hash TEXT,
            tx_hash TEXT
        )
    """)
    conn.commit()
    conn.close()

# ------------------ Validation ------------------
def is_outlier(temp, hum, soil, co2):
    if temp < -10 or temp > 60: return True
    if hum < 0 or hum > 100: return True
    if soil < 0 or soil > 100: return True
    if co2 < 0 or co2 > 100: return True
    return False

# ------------------ Insert ------------------
def insert_reading(data):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        temp = float(data.get("temperature", 0))
        hum  = float(data.get("humidity", 0))
        soil = float(data.get("soil_moisture", 0))
        co2  = float(data.get("co2_proxy", 0))

        if is_outlier(temp, hum, soil, co2):
            print(f"⚠️ Outlier skipped: {data}")
            return False

        cursor.execute("""
            INSERT INTO readings (area_id, temperature, humidity, soil_moisture, co2_proxy, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.get("area_id", "UNKNOWN"),
            temp, hum, soil, co2,
            time.strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
        print(f"💾 Data inserted: {data}")
        return True
    except Exception as e:
        print(f"❌ Insert error: {e}")
        return False
    finally:
        conn.close()

# ------------------ Batching ------------------
buffer = []
buffer_lock = Lock()
last_commit_time = time.time()

BATCH_WINDOW_SEC = 60
SCHEDULER_INTERVAL_SEC = 10

def commit_batch():
    global last_commit_time
    with buffer_lock:
        if not buffer:
            last_commit_time = time.time()
            print("⏭️ No data in buffer; skipping commit.")
            return
        batch = list(buffer)
        buffer.clear()

    # Canonical JSON -> deterministic SHA-256
    batch_json = json.dumps(batch, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    batch_hash = hashlib.sha256(batch_json).hexdigest()

    # Label batch
    area_id = batch[0].get("area_id", "UNKNOWN")
    ts = int(time.time())

    txh_hex = None
    try:
        nonce = w3.eth.get_transaction_count(DEPLOYER_ADDRESS)
        tx = registry.functions.commitHash(batch_hash, area_id, ts).build_transaction({
            "from": DEPLOYER_ADDRESS,
            "nonce": nonce,
            "gas": 300000,
            "gasPrice": w3.eth.gas_price,
            "chainId": CHAIN_ID
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=DEPLOYER_PRIVATE_KEY)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(txh)
        txh_hex = txh.hex()
        print(f"✅ On-chain commit OK | items={len(batch)} | tx={txh_hex} | block={receipt.blockNumber}")
    except Exception as e:
        print(f"❌ On-chain commit failed: {e}")

    # Persist batch metadata
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO batches (committed_at, count, batch_hash, tx_hash)
            VALUES (?, ?, ?, ?)
        """, (time.strftime('%Y-%m-%d %H:%M:%S'), len(batch), batch_hash, txh_hex))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Batch DB save failed: {e}")

    last_commit_time = time.time()

def scheduler_tick():
    elapsed = time.time() - last_commit_time
    if elapsed >= BATCH_WINDOW_SEC:
        print(f"⏱️ {int(elapsed)}s elapsed since last commit -> committing batch...")
        commit_batch()
    Timer(SCHEDULER_INTERVAL_SEC, scheduler_tick).start()

# ------------------ API Routes ------------------
@app.route("/")
def home():
    return "🌱 MRV server running (fast demo mode: 60s batch window)."

@app.route("/api/upload", methods=["POST"])
def upload():
    try:
        if not request.is_json:
            return jsonify({"error":"content-type must be application/json"}), 415
        if request.content_length and request.content_length > 256*1024:
            return jsonify({"error":"payload too large"}), 413

        payload = request.get_json()
        items = [payload] if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return jsonify({"error":"expected object or list"}), 400

        stored = 0
        for entry in items:
            for f in ["area_id","temperature","humidity","soil_moisture","co2_proxy"]:
                if f not in entry:
                    return jsonify({"error": f"missing field {f}"}), 400
            if insert_reading(entry):
                with buffer_lock:
                    buffer.append(entry)
                stored += 1

        return jsonify({"ok": True, "stored": stored, "buffer_size": len(buffer)}), 200
    except Exception as e:
        print(f"❌ Upload error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/readings", methods=["GET"])
def get_readings():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
      SELECT area_id, temperature, humidity, soil_moisture, co2_proxy, timestamp
      FROM readings ORDER BY id DESC LIMIT 50
    """)
    rows = cursor.fetchall()
    conn.close()
    data_list = [{"area_id": r[0], "temperature": r[1], "humidity": r[2],
                  "soil_moisture": r[3], "co2_proxy": r[4], "timestamp": r[5]} for r in rows]
    return jsonify(data_list)

@app.route("/api/batches", methods=["GET"])
def get_batches():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
      SELECT id, committed_at, count, batch_hash, tx_hash
      FROM batches ORDER BY id DESC LIMIT 20
    """)
    rows = cursor.fetchall()
    conn.close()
    out = [{"id": r[0], "committed_at": r[1], "count": r[2], "batch_hash": r[3], "tx_hash": r[4]} for r in rows]
    return jsonify(out)

@app.route("/api/buffer", methods=["GET"])
def get_buffer():
    with buffer_lock:
        return jsonify(buffer)

# ------------------ Main ------------------
if __name__ == "__main__":
    init_db()

    # serve the read-only verifier dashboard at /verifier
    app.register_blueprint(verifier_bp, url_prefix="/verifier")

    # guard background scheduler for debug reloader
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler_tick()

    app.run(host="0.0.0.0", port=5000, debug=True)  # open http://localhost:5000/verifier/

# verifier.py
import json, hashlib, csv, io, time
from flask import Blueprint, jsonify, request, send_file, render_template
from web3 import Web3
import sqlite3
from datetime import datetime

DB_FILE = "mrv_data.db"

RPC_URL = "http://127.0.0.1:7545"
CHAIN_ID = 1337
REGISTRY_ADDRESS_RAW = "0x9bFAC7759Dc8c2e859C042c1efc81f6335369Fdf"
DEPLOYER_ADDRESS_RAW = "0x8fca7d673a953Bc940BfcC1Fc15D72fCC7BB0662"

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

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
REGISTRY_ADDRESS = Web3.to_checksum_address(REGISTRY_ADDRESS_RAW)
registry = w3.eth.contract(address=REGISTRY_ADDRESS, abi=CONTRACT_ABI)

verifier_bp = Blueprint("verifier", __name__, template_folder="templates")

def rows_to_dicts(cursor, rows):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]

def get_db():
    return sqlite3.connect(DB_FILE)

def canonical_sha256(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

def fetch_batch(batch_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, committed_at, count, batch_hash, tx_hash FROM batches WHERE id=?", (batch_id,))
    row = cur.fetchone()
    conn.close()
    return row

def fetch_batch_readings(batch_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id, committed_at FROM batches WHERE id=?", (batch_id,))
    thisb = cur.fetchone()
    if not thisb:
        conn.close()
        return []

    cur.execute("SELECT committed_at FROM batches WHERE id < ? ORDER BY id DESC LIMIT 1", (batch_id,))
    prev = cur.fetchone()

    if prev:
        cur.execute("""
        SELECT area_id, temperature, humidity, soil_moisture, co2_proxy, timestamp
        FROM readings
        WHERE datetime(timestamp) > datetime(?) AND datetime(timestamp) <= datetime(?)
        ORDER BY id ASC
        """, (prev[0], thisb[1]))
    else:
        cur.execute("""
        SELECT area_id, temperature, humidity, soil_moisture, co2_proxy, timestamp
        FROM readings
        WHERE datetime(timestamp) <= datetime(?)
        ORDER BY id ASC
        """, (thisb[1],))

    rows = cur.fetchall()
    conn.close()

    arr = []
    for r in rows:
        arr.append({
            "area_id": r[0],
            "temperature": float(r[1]),
            "humidity": float(r[2]),
            "soil_moisture": float(r[3]),
            "co2_proxy": float(r[4]),
            "timestamp": r[5],
        })
    return arr

@verifier_bp.route("/")
def dashboard():
    return render_template("verifier.html",
                           contract=str(REGISTRY_ADDRESS),
                           chain_id=CHAIN_ID,
                           rpc_url=RPC_URL)

@verifier_bp.route("/api/batches", methods=["GET"])
def api_batches():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, committed_at, count, batch_hash, tx_hash
      FROM batches ORDER BY id DESC LIMIT 100
    """)
    rows = rows_to_dicts(cur, cur.fetchall())
    conn.close()
    return jsonify(rows)

@verifier_bp.route("/api/readings", methods=["GET"])
def api_readings():
    bid = request.args.get("batch_id", type=int)
    if not bid:
        return jsonify({"error": "batch_id required"}), 400
    fmt = request.args.get("format", default="json")

    data = fetch_batch_readings(bid)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["area_id","temperature","humidity","soil_moisture","co2_proxy","timestamp"])
        writer.writeheader()
        for it in data:
            writer.writerow(it)
        mem = io.BytesIO(output.getvalue().encode("utf-8"))
        return send_file(mem, as_attachment=True, download_name=f"batch_{bid}.csv", mimetype="text/csv")

    return jsonify(data)

@verifier_bp.route("/api/hash", methods=["GET"])
def api_hash():
    bid = request.args.get("batch_id", type=int)
    if not bid:
        return jsonify({"error": "batch_id required"}), 400

    row = fetch_batch(bid)
    if not row:
        return jsonify({"error": "batch not found"}), 404
    _, committed_at, cnt, stored_hash, txh = row

    arr = fetch_batch_readings(bid)
    recomputed = canonical_sha256(arr)
    return jsonify({
        "batch_id": bid,
        "committed_at": committed_at,
        "item_count_recorded": cnt,
        "item_count_reconstructed": len(arr),
        "stored_hash": stored_hash,
        "recomputed_hash": recomputed,
        "match": (stored_hash == recomputed),
        "tx_hash": txh
    })

@verifier_bp.route("/api/proof", methods=["GET"])
def api_proof():
    bid = request.args.get("batch_id", type=int)
    if not bid:
        return jsonify({"error": "batch_id required"}), 400

    row = fetch_batch(bid)
    if not row:
        return jsonify({"error": "batch not found"}), 404
    _, committed_at, cnt, stored_hash, txh = row

    proof = {
        "batch_id": bid,
        "committed_at": committed_at,
        "count": cnt,
        "batch_hash": stored_hash,
        "tx_hash": txh,
        "contract_address": str(REGISTRY_ADDRESS),
        "chainId": CHAIN_ID
    }

    try:
        if txh:
            # use tx hash string directly (no bytes conversion)
            receipt = w3.eth.get_transaction_receipt(txh)
            tx = w3.eth.get_transaction(txh)
            proof.update({
                "blockNumber": receipt.blockNumber,
                "from": tx["from"],
                "gasUsed": receipt.gasUsed,
                "status": receipt.status
            })
    except Exception as e:
        proof["warning"] = f"could not fetch receipt: {e}"

    return jsonify(proof)

# app.py
import os
import json
import time
import uuid
from functools import wraps
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    send_from_directory, jsonify, flash
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from flask_socketio import SocketIO, emit, join_room, leave_room, rooms as socket_rooms

# ---- Configuration ----
APP_ROOT = Path(__file__).parent
UPLOAD_FOLDER = APP_ROOT / "static" / "uploads"
IMAGE_FOLDER = APP_ROOT / "static" / "images"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "Qwerty123UIOP...bro")  # user-provided worker password

MAX_ROOM_SIZE = 10
TICK_RATE = 1/20  # not used for server tick, movement is client-driven

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
socketio = SocketIO(app, cors_allowed_origins="*")

# ---- In-memory stores (demo/prototype) ----
USERS = {}   # username -> {password_hash, is_worker(bool), coins, home_pos, inventory:[], skin}
LOGGED_IN = {}  # session_id -> username

# Rooms structure: list of rooms; each room is dict {id, users: [usernames], created_at, world_state}
ROOMS = []
GLOBAL_STORE = {
    # "items": [ {id, type: 'skin'|'item'|'wallpaper', title, desc, price, filename, options... , total_sold, limit (optional)} ]
    "items": []
}

# per-room purchases counts (room_id -> {item_id: count})
ROOM_PURCHASE_COUNTS = {}

# helper: find or create a room with < MAX_ROOM_SIZE
def assign_room_for_user(username):
    # find room with < MAX_ROOM_SIZE
    for room in ROOMS:
        if len(room["users"]) < MAX_ROOM_SIZE:
            room["users"].append(username)
            ROOM_PURCHASE_COUNTS.setdefault(room["id"], {})
            return room
    # make new room
    room_id = str(uuid.uuid4())
    new_room = {
        "id": room_id,
        "users": [username],
        "created_at": time.time(),
        # world_state can include player positions, map, items placed
        "world_state": {
            "players": {},  # username -> {x,y,skin,...}
            "placed": []
        }
    }
    ROOMS.append(new_room)
    ROOM_PURCHASE_COUNTS.setdefault(room_id, {})
    return new_room

def remove_user_from_room(username):
    for room in ROOMS:
        if username in room["users"]:
            room["users"].remove(username)
            room["world_state"]["players"].pop(username, None)
            # if empty room, remove
            if len(room["users"]) == 0:
                ROOMS.remove(room)
                ROOM_PURCHASE_COUNTS.pop(room["id"], None)
            return

def get_room_for_user(username):
    for room in ROOMS:
        if username in room["users"]:
            return room
    return None

# ---- Simple auth helpers ----
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapped

# ---- Routes ----
@app.route("/")
def index():
    # Simple landing page with login/signup forms
    return render_template("index.html")

@app.route("/signup", methods=["POST"])
def signup():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    worker_pw = request.form.get("worker_pw", "")
    if not username or not password:
        flash("Please provide username and password")
        return redirect(url_for("index"))
    if username in USERS:
        flash("Username already exists")
        return redirect(url_for("index"))
    pw_hash = generate_password_hash(password)
    is_worker = False
    if worker_pw and worker_pw == WORKER_SECRET:
        is_worker = True
    USERS[username] = {
        "password_hash": pw_hash,
        "is_worker": is_worker,
        "coins": 100,
        "home_pos": {"x": 100 + len(USERS)*20, "y": 100},  # simple deterministic home placement
        "inventory": [],
        "skin": "Pinky Sprite.png",
        "joined_at": time.time()
    }
    session["username"] = username
    # assign room
    room = assign_room_for_user(username)
    session["room_id"] = room["id"]
    return redirect(url_for("game"))

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if username not in USERS:
        flash("Incorrect username or password")
        return redirect(url_for("index"))
    if not check_password_hash(USERS[username]["password_hash"], password):
        flash("Incorrect username or password")
        return redirect(url_for("index"))
    session["username"] = username
    room = get_room_for_user(username)
    if not room:
        room = assign_room_for_user(username)
    session["room_id"] = room["id"]
    return redirect(url_for("game"))

@app.route("/logout")
@login_required
def logout():
    username = session.pop("username", None)
    session.pop("room_id", None)
    if username:
        remove_user_from_room(username)
    return redirect(url_for("index"))

@app.route("/game")
@login_required
def game():
    username = session["username"]
    room = get_room_for_user(username)
    # send store and some state to template
    return render_template("game.html",
                           username=username,
                           room_id=room["id"],
                           items_json=json.dumps(GLOBAL_STORE["items"]),
                           user_json=json.dumps(USERS.get(username)),
                           room_users=json.dumps(room["users"]))

# Worker-only page to add items / skins / wallpapers
@app.route("/worker", methods=["GET", "POST"])
@login_required
def worker_page():
    username = session["username"]
    user = USERS.get(username)
    if not user or not user.get("is_worker"):
        flash("Not authorized")
        return redirect(url_for("game"))
    if request.method == "POST":
        # add item to global store
        itype = request.form.get("type", "item")  # skin|item|wallpaper
        title = request.form.get("title", "Untitled")
        desc = request.form.get("desc", "")
        price = int(request.form.get("price", 0))
        limit = request.form.get("limit")
        limit = int(limit) if limit and limit.isdigit() else None
        held = bool(request.form.get("held"))
        # file
        f = request.files.get("image")
        filename = None
        if f:
            fn = secure_filename(f.filename)
            filename = f"{uuid.uuid4().hex}_{fn}"
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        new_item = {
            "id": str(uuid.uuid4()),
            "type": itype,
            "title": title,
            "desc": desc,
            "price": price,
            "limit": limit,
            "filename": filename,
            "created_at": time.time(),
            "total_sold": 0,
            "options": {
                "held": held,
                "gravity": bool(request.form.get("gravity")),
                "can_store": bool(request.form.get("can_store")),
                "robot": bool(request.form.get("robot")),
                # basic robot options encoded as simple flags
                "robot_behaviors": {
                    "follow_owner": bool(request.form.get("robot_follow")),
                    "give_items": bool(request.form.get("robot_give")),
                    "auto_attack": bool(request.form.get("robot_attack"))
                }
            }
        }
        GLOBAL_STORE["items"].insert(0, new_item)  # newest first
        flash("Item added to Cube Mall (online for all servers).")
        return redirect(url_for("worker_page"))
    # GET
    return render_template("worker.html", items=GLOBAL_STORE["items"])

# Purchase endpoint
@app.route("/buy/<item_id>", methods=["POST"])
@login_required
def buy(item_id):
    username = session["username"]
    user = USERS[username]
    # find item
    item = next((it for it in GLOBAL_STORE["items"] if it["id"] == item_id), None)
    if not item:
        return jsonify({"success": False, "msg": "Item not found"}), 404
    room_id = session.get("room_id")
    # check limit
    if item["limit"]:
        count = ROOM_PURCHASE_COUNTS.setdefault(room_id, {}).get(item_id, 0)
        if count >= item["limit"]:
            return jsonify({"success": False, "msg": "Item sold out in this server"}), 400
    if user["coins"] < item["price"]:
        return jsonify({"success": False, "msg": "Not enough coins"}), 400
    user["coins"] -= item["price"]
    user["inventory"].append(item_id)
    item["total_sold"] += 1
    ROOM_PURCHASE_COUNTS.setdefault(room_id, {})[item_id] = ROOM_PURCHASE_COUNTS.setdefault(room_id, {}).get(item_id, 0) + 1
    return jsonify({"success": True, "coins": user["coins"]})

# Serve uploaded images
@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(str(UPLOAD_FOLDER), filename)

# ---- Socket.IO events for real-time multiplayer ----
@socketio.on("connect")
def on_connect():
    sid = request.sid
    # we'll rely on session cookie to map user
    username = session.get("username")
    if username:
        LOGGED_IN[sid] = username
        room = get_room_for_user(username)
        if room:
            join_room(room["id"])
            # add player state
            room["world_state"]["players"][username] = {
                "x": USERS[username]["home_pos"]["x"],
                "y": USERS[username]["home_pos"]["y"],
                "skin": USERS[username]["skin"],
                "last_active": time.time(),
                "in_house": False
            }
            # notify others
            emit("player_join", {"username": username, "state": room["world_state"]["players"][username]}, room=room["id"])
            # send current room state to connecting client
            emit("room_state", {
                "room_id": room["id"],
                "players": room["world_state"]["players"],
                "items": GLOBAL_STORE["items"],
                "your_username": username,
                "coins": USERS[username]["coins"]
            })

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    username = LOGGED_IN.pop(sid, None)
    if username:
        # remove from room world state
        room = get_room_for_user(username)
        if room:
            room["world_state"]["players"].pop(username, None)
            emit("player_leave", {"username": username}, room=room["id"])
        # We keep the user in ROOMS list until they logout (so they return to same server on next login).
        # If you'd rather remove them from room entirely on disconnect, call remove_user_from_room(username).

@socketio.on("move")
def on_move(data):
    # data: {x, y}
    sid = request.sid
    username = LOGGED_IN.get(sid)
    if not username:
        return
    room = get_room_for_user(username)
    if not room:
        return
    x = float(data.get("x", 0))
    y = float(data.get("y", 0))
    # clamp or accept
    player = room["world_state"]["players"].get(username, {})
    player.update({"x": x, "y": y, "last_active": time.time()})
    room["world_state"]["players"][username] = player
    emit("player_moved", {"username": username, "x": x, "y": y}, room=room["id"], include_self=False)

@socketio.on("chat")
def on_chat(data):
    # data: {msg}
    sid = request.sid
    username = LOGGED_IN.get(sid)
    if not username:
        return
    room = get_room_for_user(username)
    if not room:
        return
    msg = data.get("msg", "")[:300]
    # broadcast chat to room; front-end will display bubble above head for 5s
    payload = {
        "from": username,
        "msg": msg,
        "timestamp": time.time()
    }
    emit("chat_bubble", payload, room=room["id"])

@socketio.on("press_e")
def on_press_e(data):
    # handle entering home or interacting with an item
    sid = request.sid
    username = LOGGED_IN.get(sid)
    if not username:
        return
    room = get_room_for_user(username)
    if not room:
        return
    action = data.get("action")
    if action == "enter_home":
        # check distance between player and their home; we assume client checks too
        # toggle in_house
        player = room["world_state"]["players"].get(username)
        if player:
            player["in_house"] = True
            emit("entered_house", {"username": username}, room=room["id"])
    elif action == "exit_home":
        player = room["world_state"]["players"].get(username)
        if player:
            player["in_house"] = False
            emit("exited_house", {"username": username}, room=room["id"])

@socketio.on("party_invite")
def on_party_invite(data):
    # {host}
    sid = request.sid
    host = LOGGED_IN.get(sid)
    if not host:
        return
    room = get_room_for_user(host)
    if not room:
        return
    # send a party notification to everyone in the room
    payload = {
        "host": host,
        "msg": f"JOIN {host}'S PARTY!!! DO YOU WANT TO COME?"
    }
    emit("party_notification", payload, room=room["id"])

@socketio.on("party_response")
def on_party_response(data):
    # {host, response: "accept"|"decline", from}
    sid = request.sid
    username = LOGGED_IN.get(sid)
    if not username:
        return
    host = data.get("host")
    response = data.get("response")
    room = get_room_for_user(host)
    if not room:
        # send failure
        emit("party_failed", {"msg": "Host not in same server or gone."}, room=request.sid)
        return
    if response == "accept":
        # move joining player into host's house (set in_house True) and notify
        room["world_state"]["players"].setdefault(username, {})["in_house"] = True
        emit("joined_party", {"host": host, "guest": username}, room=room["id"])
    else:
        # decline simply notifies the host and disappears
        emit("party_declined", {"host": host, "guest": username}, room=room["id"])

@socketio.on("tap_player")
def on_tap_player(data):
    # {target_username, action: "attack"|"donate", amount: int}
    sid = request.sid
    username = LOGGED_IN.get(sid)
    if not username:
        return
    target = data.get("target")
    action = data.get("action")
    room = get_room_for_user(username)
    if not room or target not in room["users"]:
        return
    if action == "donate":
        amount = int(data.get("amount", 0))
        if amount <= 0 or USERS[username]["coins"] < amount:
            emit("donate_failed", {"msg": "Invalid amount or insufficient coins"}, room=request.sid)
            return
        USERS[username]["coins"] -= amount
        USERS[target]["coins"] += amount
        emit("donation", {"from": username, "to": target, "amount": amount}, room=room["id"])
    elif action == "attack":
        # Attacks are only allowed when attacker has an item in inventory that permits it.
        # For simplicity, we don't track item usage here — front-end should check. Notify clients so they can show damage.
        emit("attacked", {"from": username, "to": target, "damage": 10}, room=room["id"])

# Debug: get server state
@app.route("/_status")
def status():
    return jsonify({
        "users": list(USERS.keys()),
        "rooms": [{ "id": r["id"], "users": r["users"] } for r in ROOMS],
        "store_items": len(GLOBAL_STORE["items"])
    })

# ---- Templates (simple, in-file for demo) ----
# For brevity in this single-file demo we will write small templates to templates/ if not present.
TEMPLATES_DIR = APP_ROOT / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)

INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cubeo Land — login / signup</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; display:flex; gap:20px; padding:40px; }
    form { border:1px solid #ccc; padding:20px; border-radius:8px; width:320px; }
    input { width:100%; padding:8px; margin:6px 0; }
    .hint { font-size:12px; color:#666; }
  </style>
</head>
<body>
  <div>
    <h2>Sign Up</h2>
    <form action="/signup" method="post">
      <input name="username" placeholder="username" required>
      <input name="password" type="password" placeholder="password" required>
      <div class="hint">Optional: become a worker (enter worker password)</div>
      <input name="worker_pw" placeholder="worker secret (optional)">
      <button type="submit">OKAY (Sign up)</button>
    </form>
  </div>
  <div>
    <h2>Login</h2>
    <form action="/login" method="post">
      <input name="username" placeholder="username" required>
      <input name="password" type="password" placeholder="password" required>
      <button type="submit">Login</button>
    </form>
  </div>
  <div>
    <h2>Demo notes</h2>
    <p>Default coins: 100. Images must be uploaded to <code>static/images/</code>.</p>
  </div>
</body>
</html>
"""

GAME_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Cubeo Land — Game</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body { height:100%; margin:0; overflow:hidden; font-family:Arial,Helvetica,sans-serif;}
  #game { position:relative; width:100vw; height:100vh; background: url('/static/images/blue.png') repeat; touch-action:none; }
  .player { position:absolute; width:48px; height:48px; transform:translate(-50%,-50%); pointer-events:none; }
  .bubble { position:absolute; transform:translate(-50%,-100%); background:rgba(255,255,255,0.9); padding:6px 10px; border-radius:12px; font-size:14px; }
  #ui { position:absolute; right:12px; top:12px; display:flex; flex-direction:column; gap:8px; }
  #chatbox { position:absolute; left:12px; bottom:12px; display:flex; gap:8px; }
  input[type=text] { padding:8px; width:320px; }
  button { padding:8px 10px; }
  .home { position:absolute; width:28px; height:28px; border-radius:50%; background:yellow; transform:translate(-50%,-50%); opacity:0.9; display:flex; align-items:center; justify-content:center; font-weight:bold; }
  #phone { position:absolute; left:50%; top:10%; transform:translate(-50%,0); width:90%; height:80%; background: white; border-radius:12px; display:none; flex-direction:column; }
  #mall { display:none; width:100%; height:100%; overflow:hidden; }
  #mall .row { display:flex; gap:12px; padding:12px; overflow-x:auto; white-space:nowrap; }
  .mall-card { width:180px; border:1px solid #ccc; padding:8px; border-radius:8px; text-align:center; }
  .violet { background:#8a2be2; color:white; padding:8px; border-radius:8px; }
  .cyan { background:#00ffff; padding:8px; border-radius:8px; }
</style>
</head>
<body>
<div id="game"></div>

<div id="ui">
  <div>Player: <strong id="me">{{username}}</strong></div>
  <div>Coins: <span id="coins">0</span></div>
  <button id="partyBtn" class="violet">PARTY!!!</button>
  <button id="phoneBtn" class="cyan">PHONE</button>
  <a href="/worker">Worker Editor</a>
  <a href="/logout">Logout</a>
</div>

<div id="chatbox">
  <input id="chatInput" type="text" placeholder="Say something...">
  <button id="sendBtn">Send</button>
</div>

<!-- Phone UI -->
<div id="phone">
  <div style="padding:8px; display:flex; justify-content:space-between;">
    <strong>Phone</strong>
    <button id="phoneBack">Back</button>
  </div>
  <div style="padding:12px;">
    <button id="cubeMedia">Cube Media</button>
    <button id="cubeMallBtn">CUBE MALL (Online)</button>
  </div>
  <div id="mall">
    <div style="padding:8px;">Coins: <span id="mallCoins">0</span></div>
    <div class="row" id="newRow"></div>
    <h3 style="padding-left:12px;">Popular</h3>
    <div class="row" id="popularRow"></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"
        integrity="sha512-kb4GQF+4oQnQk+0bq1UQ8cM41mZrU3J0FLqQF4ePgrb6tY0zJcK0YzqAU3VZqV6m3ovz8m9M9k6xQj5Mlyw1wQ=="
        crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script>
const socket = io();
const username = "{{username}}";
const roomId = "{{room_id}}";
const meSpan = document.getElementById("me");
const coinsSpan = document.getElementById("coins");
let myCoins = 0;
let players = {}; // username -> DOM node + state

const game = document.getElementById("game");

function createPlayerNode(name, state){
  let el = document.createElement("div");
  el.className = "player";
  const img = document.createElement("img");
  img.src = "/static/images/Pinky Sprite.png";
  img.style.width = "48px";
  img.style.height = "48px";
  el.appendChild(img);
  // name label
  const nameLbl = document.createElement("div");
  nameLbl.style.position="absolute";
  nameLbl.style.top="54px";
  nameLbl.style.left="50%";
  nameLbl.style.transform="translateX(-50%)";
  nameLbl.style.fontSize="12px";
  nameLbl.style.color="#fff";
  nameLbl.innerText = name;
  el.appendChild(nameLbl);
  game.appendChild(el);
  return el;
}

function updatePlayerNode(name, state){
  let entry = players[name];
  if(!entry){
    const node = createPlayerNode(name, state);
    players[name] = {node, state};
    entry = players[name];
  }
  const el = entry.node;
  el.style.left = (state.x || 100) + "px";
  el.style.top = (state.y || 100) + "px";
  // remove bubble if in house or hidden
  if(state.in_house){
    el.style.opacity = 0.4;
  } else {
    el.style.opacity = 1;
  }
  entry.state = state;
}

// handle room_state on connect
socket.on("room_state", (data) => {
  // data.players, data.items
  myCoins = data.coins || 0;
  coinsSpan.innerText = myCoins;
  document.getElementById("mallCoins").innerText = myCoins;
  // render players
  Object.entries(data.players).forEach(([name, st]) => {
    updatePlayerNode(name, st);
  });
  // homes: one home per player
  renderHomes(Object.keys(data.players));
});

// a player moved
socket.on("player_moved", (d) => {
  const p = players[d.username];
  if(p){
    p.node.style.left = d.x + "px";
    p.node.style.top = d.y + "px";
  }
});

// player join/leave
socket.on("player_join", (d) => {
  updatePlayerNode(d.username, d.state);
  renderHomes(Object.keys(players));
});
socket.on("player_leave", (d) => {
  const name = d.username;
  if(players[name]){
    players[name].node.remove();
    delete players[name];
  }
  renderHomes(Object.keys(players));
});

// chat bubble event
socket.on("chat_bubble", (d) => {
  // find player's node and display bubble above head for 5 seconds
  const name = d.from;
  if(!players[name]) return;
  const node = players[name].node;
  // create bubble
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerText = d.msg;
  bubble.style.left = node.style.left;
  bubble.style.top = (parseFloat(node.style.top) - 30) + "px";
  game.appendChild(bubble);
  setTimeout(()=> bubble.remove(), 5000);
});

// party notifications
socket.on("party_notification", (d) => {
  // show confirm-like notification with accept/decline
  const notif = document.createElement("div");
  notif.style.position="absolute";
  notif.style.left="50%";
  notif.style.top="20%";
  notif.style.transform="translate(-50%,0)";
  notif.style.background="#ffd";
  notif.style.padding="12px";
  notif.style.border="1px solid #333";
  notif.innerHTML = `<div>${d.msg}</div>`;
  const accept = document.createElement("button");
  accept.innerText="Accept";
  const decline = document.createElement("button");
  decline.innerText="Decline";
  notif.appendChild(accept);
  notif.appendChild(decline);
  game.appendChild(notif);
  const tid = setTimeout(()=> { notif.remove(); }, 10000);
  accept.onclick = () => {
    socket.emit("party_response", {host: d.host || d.host, response:"accept"});
    clearTimeout(tid);
    notif.remove();
  }
  decline.onclick = () => {
    socket.emit("party_response", {host: d.host || d.host, response:"decline"});
    clearTimeout(tid);
    notif.remove();
  }
});

// joined party
socket.on("joined_party", (d) => {
  alert(d.guest + " joined " + d.host + "'s party! You are teleported into host's house.");
});

// tap/donate/attack events - simple UI notifications
socket.on("donation", (d) => {
  if(d.to === username) {
    alert(`${d.from} donated ${d.amount} coins to you!`);
    myCoins += d.amount;
    coinsSpan.innerText = myCoins;
  } else if (d.from === username){
    myCoins -= d.amount;
    coinsSpan.innerText = myCoins;
  }
});
socket.on("attacked", (d) => {
  // show damage icon above target
  const tgt = players[d.to];
  if(tgt){
    const dmg = document.createElement("div");
    dmg.style.position="absolute";
    dmg.style.left = (parseFloat(tgt.node.style.left) + 10) + "px";
    dmg.style.top = (parseFloat(tgt.node.style.top) - 20) + "px";
    dmg.innerText = "-" + d.damage;
    game.appendChild(dmg);
    setTimeout(()=> dmg.remove(), 2000);
  }
});

// homes rendering
let homeNodes = {};
function renderHomes(userlist){
  // clear
  Object.values(homeNodes).forEach(n => n.remove());
  homeNodes = {};
  const i = 0;
  let idx = 0;
  userlist.forEach((name, idx)=>{
    // compute simple home positions
    const x = 50 + idx*80;
    const y = 400;
    const home = document.createElement("div");
    home.className = "home";
    home.style.left = x + "px";
    home.style.top = y + "px";
    home.innerText = (idx+1);
    game.appendChild(home);
    home.onclick = () => {
      if(name === username){
        // enter home
        socket.emit("press_e",{action:"enter_home"});
        alert("Entered your home!");
      } else {
        alert("You can only enter your own home!");
      }
    }
    homeNodes[name] = home;
  });
}

// simple movement controls
let pos = {x: 200, y: 200};
function sendMove(){
  socket.emit("move", pos);
  updatePlayerNode(username, pos);
}
document.addEventListener("keydown", (e)=>{
  const step = 8;
  if(e.key === "w" || e.key === "W" || e.key === "ArrowUp"){ pos.y -= step; sendMove(); }
  if(e.key === "s" || e.key === "S" || e.key === "ArrowDown"){ pos.y += step; sendMove(); }
  if(e.key === "a" || e.key === "A" || e.key === "ArrowLeft"){ pos.x -= step; sendMove(); }
  if(e.key === "d" || e.key === "D" || e.key === "ArrowRight"){ pos.x += step; sendMove(); }
  if(e.key === "e" || e.key === "E"){ socket.emit("press_e", {action:"enter_home"}); }
});

// touch swipe for mobile
let touchStart = null;
game.addEventListener("touchstart", (ev)=>{
  if(ev.touches && ev.touches[0]){
    touchStart = {x:ev.touches[0].clientX, y:ev.touches[0].clientY};
  }
});
game.addEventListener("touchend", (ev)=>{
  if(!touchStart) return;
  const t = ev.changedTouches[0];
  const dx = t.clientX - touchStart.x;
  const dy = t.clientY - touchStart.y;
  const absx = Math.abs(dx), absy = Math.abs(dy);
  const step = 80;
  if(absx > absy){
    pos.x += dx > 0 ? step : -step;
  } else {
    pos.y += dy > 0 ? step : -step;
  }
  sendMove();
  touchStart = null;
});

// Chat
document.getElementById("sendBtn").onclick = sendChat;
document.getElementById("chatInput").addEventListener("keydown", (e)=> { if(e.key === "Enter") sendChat(); });
function sendChat(){
  const txt = document.getElementById("chatInput").value.trim();
  if(!txt) return;
  socket.emit("chat", {msg: txt});
  document.getElementById("chatInput").value = "";
}

// Party button
document.getElementById("partyBtn").onclick = ()=>{
  socket.emit("party_invite", {host: username});
}

// Phone UI
const phone = document.getElementById("phone");
document.getElementById("phoneBtn").onclick = ()=> { phone.style.display = "flex"; document.getElementById("mall").style.display = "none"; }
document.getElementById("phoneBack").onclick = ()=> { phone.style.display = "none"; }
document.getElementById("cubeMallBtn").onclick = ()=> {
  document.getElementById("mall").style.display = "block";
  loadMall();
}

function loadMall(){
  // for demo: we fetch items by embedding them in page from server in initial render (but can also fetch / get via socket)
  const items = {{ items_json|tojson }};
  const newRow = document.getElementById("newRow");
  newRow.innerHTML = "";
  items.forEach(it=>{
    const card = document.createElement("div");
    card.className = "mall-card";
    const img = document.createElement("img");
    img.style.width="100%";
    img.style.height="100px";
    img.src = it.filename ? "/uploads/"+it.filename : "/static/images/Pinky Sprite.png";
    card.appendChild(img);
    const title = document.createElement("div"); title.innerText = it.title;
    const desc = document.createElement("div"); desc.innerText = it.desc;
    const price = document.createElement("div"); price.innerText = "Price: "+it.price;
    const buy = document.createElement("button"); buy.innerText = "Buy";
    buy.onclick = async ()=>{
      const res = await fetch("/buy/"+it.id, {method:"POST"});
      const js = await res.json();
      if(js.success){
        alert("Bought! Coins left: "+js.coins);
        myCoins = js.coins;
        coinsSpan.innerText = myCoins;
        document.getElementById("mallCoins").innerText = myCoins;
      } else {
        alert(js.msg || "Failed to buy");
      }
    }
    card.appendChild(title); card.appendChild(desc); card.appendChild(price); card.appendChild(buy);
    newRow.appendChild(card);
  });
}

// small initial move to populate me
setTimeout(()=>{ sendMove(); }, 500);

</script>
</body>
</html>
"""

WORKER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Worker Editor</title></head><body>
<h2>Worker Editor</h2>
<form method="post" enctype="multipart/form-data">
  <label>Type:
    <select name="type">
      <option value="skin">Skin</option>
      <option value="item">Item</option>
      <option value="wallpaper">Wallpaper</option>
    </select>
  </label><br>
  <label>Title: <input name="title" required></label><br>
  <label>Description: <input name="desc"></label><br>
  <label>Price: <input name="price" type="number" value="0"></label><br>
  <label>Limit (optional): <input name="limit" type="number"></label><br>
  <label>Image: <input name="image" type="file" accept="image/*"></label><br>
  <label>Held: <input type="checkbox" name="held"></label><br>
  <label>Gravity: <input type="checkbox" name="gravity"></label><br>
  <label>Can Store: <input type="checkbox" name="can_store"></label><br>
  <label>Robot: <input type="checkbox" name="robot"></label><br>
  <label>Robot follow: <input type="checkbox" name="robot_follow"></label><br>
  <label>Robot give items: <input type="checkbox" name="robot_give"></label><br>
  <label>Robot attack: <input type="checkbox" name="robot_attack"></label><br>
  <button type="submit">BAM! I'M DONE!</button>
</form>
<h3>Current Cube Mall Items</h3>
<ul>
{% for it in items %}
  <li><strong>{{it.title}}</strong> - {{it.desc}} ({{it.price}} coins)</li>
{% endfor %}
</ul>
<a href="/game">Back to Game</a>
</body></html>
"""

# Write templates to files if not already present
def ensure_template(path, content):
    p = TEMPLATES_DIR / path
    if not p.exists():
        p.write_text(content, encoding="utf-8")
ensure_template("index.html", INDEX_HTML)
ensure_template("game.html", GAME_HTML)
ensure_template("worker.html", WORKER_HTML)

# ---- Run ----
if __name__ == "__main__":
    # create a couple placeholder images if they're missing (transparent placeholders)
    placeholder = IMAGE_FOLDER / "Pinky Sprite.png"
    if not placeholder.exists():
        try:
            from PIL import Image
            img = Image.new("RGBA", (64,64), (255,0,255,0))
            img.save(placeholder)
        except Exception:
            pass
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

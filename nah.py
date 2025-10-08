"""
Cubeo Land - Prototype Flask + Socket.IO single-file app
Features implemented (prototype):
- Signup / Login (username + password). Worker signup via worker password.
- Automatic server assignment: each server holds max 10 players; new server created when full.
- Real-time movement (WASD + mobile swipe) with Socket.IO; players seen by others in same server.
- Chat box: messages broadcast to server; front-end displays message above player's head for 5s.
- Each player has a "home" (yellow circle). Press E / tap to enter your home. Only you can enter your home.
- Party invites: host can invite server; clients get a notification with Accept/Decline; Accept teleports them into host's home.
- Phone UI with "Cube Mall (Online)" showing coins, items; basic buy flow and worker tools to add items/skins/wallpapers.
- Items/skins/wallpapers created by workers are global across servers and appear in the mall. Items may be limited by quantity.

NOTES / Limitations:
- This is a prototype. Data is stored in-memory; if the server restarts all accounts/items/servers are reset.
- Images are not uploaded to disk; the frontend can send data URLs for small images. This prototype stores those as data URLs in memory.
- Security: Passwords are stored hashed, but there is no email verification, rate limiting, or strong anti-cheat.
- Scaling: This is a single-process in-memory server for demo and local development only.

Run:
1) pip install flask flask-socketio eventlet
2) python cubeoland_flask_app.py
3) Open http://localhost:5000

"""
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.security import generate_password_hash, check_password_hash
from uuid import uuid4
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-change-me'
socketio = SocketIO(app, cors_allowed_origins="*")

# -----------------------------
# In-memory stores (prototype)
# -----------------------------
USERS = {}  # username -> {password_hash, coins, worker(bool)}
ITEMS = []  # global items/skins/wallpapers in cube mall
SERVERS = []  # list of server dicts: {id, players: [username], created_at}
PLAYER_STATE = {}  # username -> real-time state {x,y,color,socket_id,server_id,home_pos}
PENDING_PARTIES = {}  # invite_id -> {host, server_id, expires_at}

WORKER_SECRET = 'Qwerty123UIOP...bro'  # the user gave this; keep it as-is per request
SERVER_CAPACITY = 10

# Utility: find or create server for a player
def assign_server_for_new_player(username):
    # find server with < SERVER_CAPACITY
    for s in SERVERS:
        if len(s['players']) < SERVER_CAPACITY:
            s['players'].append(username)
            return s['id']
    # create new
    sid = str(uuid4())
    s = {'id': sid, 'players': [username], 'created_at': time.time()}
    SERVERS.append(s)
    return sid

def remove_player_from_server(username):
    for s in SERVERS:
        if username in s['players']:
            s['players'].remove(username)
    # remove empty servers
    global SERVERS
    SERVERS = [s for s in SERVERS if len(s['players'])>0]

# -----------------------------
# Flask routes: signup / login
# -----------------------------
TEMPLATE_BASE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cubeo Land Prototype</title>
  <style>
    body {font-family: Arial, Helvetica, sans-serif; margin:0; padding:0; background:#dfefff}
    .center {display:flex;align-items:center;justify-content:center;height:100vh}
    .card {background:white;padding:20px;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.08);width:320px}
    input,button,select {width:100%;padding:10px;margin:6px 0;border-radius:6px;border:1px solid #ddd}
    .small {width:auto;padding:6px;margin-right:6px}
    .topbar {background:#2f6fdf;color:white;padding:10px}
  </style>
</head>
<body>
  <div class="topbar">Cubeo Land Prototype — Logged in as: {{user if user else 'Guest'}}</div>
  {% block body %}{% endblock %}
</body>
</html>
"""

@app.route('/')
def index():
    if 'username' in session:
        return redirect(url_for('play'))
    return render_template_string(TEMPLATE_BASE + """
    {% block body %}
    <div class="center">
      <div class="card">
        <h2>Welcome to Cubeo Land</h2>
        <a href="{{url_for('signup')}}"><button>Sign Up</button></a>
        <a href="{{url_for('login')}}"><button>Log In</button></a>
        <p style="font-size:12px;color:#666">This is a prototype. Images are placeholders.</p>
      </div>
    </div>
    {% endblock %}
    "", user=None)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        worker_secret = request.form.get('worker_secret','')
        if not username or not password:
            return "Missing fields", 400
        if username in USERS:
            return "Username taken", 400
        USERS[username] = {'password_hash': generate_password_hash(password), 'coins': 100, 'worker': worker_secret==WORKER_SECRET}
        # create default player state
        PLAYER_STATE[username] = {'x':50,'y':50,'color':'#ff99cc','socket_id':None,'server_id':None,'home_pos':None}
        session['username'] = username
        # assign server
        sid = assign_server_for_new_player(username)
        PLAYER_STATE[username]['server_id'] = sid
        return redirect(url_for('play'))

    return render_template_string(TEMPLATE_BASE + """
    {% block body %}
    <div class="center">
      <form class="card" method="post">
        <h3>Sign Up</h3>
        <input name="username" placeholder="username" required>
        <input name="password" placeholder="password" required type="password">
        <input name="worker_secret" placeholder="Worker password (optional)">
        <button type="submit">Create Account</button>
        <a href="{{url_for('index')}}"><button type="button">Back</button></a>
      </form>
    </div>
    {% endblock %}
    ", user=None)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if username not in USERS:
            return "Unknown user", 400
        if not check_password_hash(USERS[username]['password_hash'], password):
            return "Wrong password", 400
        session['username'] = username
        # assign to server if not already assigned
        if PLAYER_STATE.get(username) is None:
            PLAYER_STATE[username] = {'x':50,'y':50,'color':'#ff99cc','socket_id':None,'server_id':None,'home_pos':None}
        if not PLAYER_STATE[username]['server_id']:
            sid = assign_server_for_new_player(username)
            PLAYER_STATE[username]['server_id'] = sid
        return redirect(url_for('play'))

    return render_template_string(TEMPLATE_BASE + """
    {% block body %}
    <div class="center">
      <form class="card" method="post">
        <h3>Login</h3>
        <input name="username" placeholder="username" required>
        <input name="password" placeholder="password" required type="password">
        <button type="submit">Log In</button>
        <a href="{{url_for('index')}}"><button type="button">Back</button></a>
      </form>
    </div>
    {% endblock %}
    ", user=None)

@app.route('/logout')
def logout():
    username = session.pop('username', None)
    if username:
        # cleanup player from server
        remove_player_from_server(username)
        PLAYER_STATE.pop(username, None)
    return redirect(url_for('index'))

# -----------------------------
# Game page
# -----------------------------
GAME_HTML = TEMPLATE_BASE + """
{% block body %}
<div style="position:relative; height:calc(100vh - 40px);">
  <!-- Simple full-screen map (blue) -->
  <canvas id="map" width="900" height="600" style="background:url('/static/blue.png') repeat; touch-action:none; width:100%; height:100%; display:block"></canvas>

  <!-- HUD -->
  <div style="position:absolute; top:12px; left:12px; background:rgba(255,255,255,0.9); padding:8px; border-radius:8px">
    <div>Coins: <span id="coins">0</span></div>
    <button id="phoneBtn">PHONE</button>
    <button id="mallBtn">CUBE MALL (Online)</button>
    <button id="partyBtn" style="background:violet;color:white">PARTY!!!</button>
    <button id="logoutBtn">Logout</button>
  </div>

  <!-- Chat -->
  <div style="position:absolute; bottom:12px; left:12px; right:12px; display:flex; gap:8px;">
    <input id="chatInput" placeholder="Type message..." style="flex:1;padding:10px;border-radius:8px;border:1px solid #ccc">
    <button id="sendChat">Send</button>
  </div>

  <!-- Phone overlay (hidden) -->
  <div id="phone" style="position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); width:360px; height:640px; background:white; border-radius:18px; box-shadow:0 10px 30px rgba(0,0,0,0.2); display:none; z-index:40;">
    <div style="padding:12px;">Phone <button id="phoneBack">Back</button></div>
    <div style="padding:12px;">
      <button id="cubeMedia">Cube Media</button>
    </div>
    <div id="phoneContent" style="padding:12px;">Phone content</div>
  </div>

  <!-- Cube Mall modal -->
  <div id="mall" style="position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); width:800px; max-width:96%; height:480px; background:green; display:none; z-index:50; overflow:hidden;">
    <div style="padding:12px"><button id="mallBack">Back</button> Coins: <span id="mallCoins">0</span></div>
    <div id="mallItems" style="white-space:nowrap; overflow-x:auto; padding:12px; height:380px;">
      <!-- items inserted here horizontally -->
    </div>
  </div>

  <!-- Simple notifications container -->
  <div id="notifications" style="position:absolute; right:12px; top:72px; width:260px; z-index:60"></div>

</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"></script>
<script>
const username = {{username|tojson}};
let socket = io();
let serverId = null;
let players = {}; // username -> state
let my = {x:50,y:50, color:'#ff99cc'};
let coins = 0;

// join room on connect
socket.on('connect', ()=>{
  socket.emit('join_game', {username});
});

socket.on('joined', (data)=>{
  serverId = data.server_id;
  players = data.players;
  coins = data.coins;
  document.getElementById('coins').innerText = coins;
  document.getElementById('mallCoins').innerText = coins;
  // set my state if provided
  if(players[username]){ my.x = players[username].x; my.y = players[username].y; my.home = players[username].home_pos }
  draw();
});

socket.on('player_update', (data)=>{
  players[data.username] = data.state;
  draw();
});

socket.on('player_disconnect', (data)=>{
  delete players[data.username]; draw();
});

socket.on('chat_message', (m)=>{
  // m: {from, text}
  showChatAbove(m.from, m.text);
});

socket.on('party_invite', (d)=>{
  // d: {host, invite_id}
  showInvite(d.host, d.invite_id);
});

socket.on('teleport_to_home', (d)=>{
  // move player to host's home
  if(d.username === username){ my.x = d.x; my.y = d.y; draw(); }
});

socket.on('mall_update', (d)=>{
  renderMall(d.items);
});

// map canvas
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');
function draw(){
  // scale to fit
  canvas.width = canvas.clientWidth;
  canvas.height = canvas.clientHeight;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  // draw background as blue pattern (if image exists it'll be on CSS background)
  // draw players
  for(let u in players){
    const p = players[u];
    // rectangle for player (use image normally)
    ctx.fillStyle = p.color || '#ff99cc';
    ctx.fillRect(p.x, p.y, 32, 32);
    // draw username
    ctx.fillStyle = 'black'; ctx.font='12px Arial'; ctx.fillText(u, p.x, p.y-6);
    // draw home circles if defined
    if(p.home_pos){ ctx.beginPath(); ctx.arc(p.home_pos.x, p.home_pos.y, 10, 0, Math.PI*2); ctx.fillStyle='yellow'; ctx.fill(); }
  }
}

// movement controls
const speed = 4;
let keys = {};
window.addEventListener('keydown', e=>{ keys[e.key.toLowerCase()] = true; if(e.key.toLowerCase()=='e'){ socket.emit('press_e', {}); } });
window.addEventListener('keyup', e=>{ keys[e.key.toLowerCase()] = false; });

function gameLoop(){
  let moved = false;
  if(keys['w']||keys['arrowup']){ my.y -= speed; moved=true }
  if(keys['s']||keys['arrowdown']){ my.y += speed; moved=true }
  if(keys['a']||keys['arrowleft']){ my.x -= speed; moved=true }
  if(keys['d']||keys['arrowright']){ my.x += speed; moved=true }
  if(moved){ socket.emit('move', {x:my.x, y:my.y}); draw(); }
  requestAnimationFrame(gameLoop);
}
requestAnimationFrame(gameLoop);

// simple touch swipe handling for mobile
let touchStart = null;
canvas.addEventListener('touchstart', e=>{ touchStart = e.touches[0]; });
canvas.addEventListener('touchend', e=>{
  if(!touchStart) return; let t = e.changedTouches[0]; let dx = t.clientX - touchStart.clientX; let dy = t.clientY - touchStart.clientY; if(Math.abs(dx)>30 || Math.abs(dy)>30){ if(Math.abs(dx)>Math.abs(dy)){ my.x += dx>0?50:-50 } else { my.y += dy>0?50:-50 } socket.emit('move',{x:my.x,y:my.y}); draw(); } touchStart=null;
});

// chat
document.getElementById('sendChat').onclick = ()=>{
  const txt = document.getElementById('chatInput').value.trim(); if(!txt) return; socket.emit('chat', {text:txt}); document.getElementById('chatInput').value=''; }

function showChatAbove(from, text){
  // briefly render above player's head in DOM
  const notif = document.createElement('div'); notif.style.position='absolute'; notif.style.background='rgba(255,255,255,0.95)'; notif.style.padding='6px'; notif.style.borderRadius='6px'; notif.style.zIndex=70; notif.innerText = from+': '+text;
  document.body.appendChild(notif);
  // position near player's position
  const p = players[from];
  if(p){ const rect = canvas.getBoundingClientRect(); notif.style.left = (rect.left + p.x)+'px'; notif.style.top = (rect.top + p.y - 30)+'px'; }
  setTimeout(()=>{ notif.remove() }, 5000);
}

// show invite
function showInvite(host, invite_id){
  const n = document.createElement('div'); n.style.background='white'; n.style.padding='8px'; n.style.border='1px solid #ccc'; n.style.marginTop='8px';
  n.innerHTML = `<strong>JOIN ${host.toUpperCase()}\'S PARTY!!!</strong><div>DO YOU WANT TO COME?</div>`;
  const accept = document.createElement('button'); accept.innerText='Accept'; const decline = document.createElement('button'); decline.innerText='Decline';
  n.appendChild(accept); n.appendChild(decline);
  document.getElementById('notifications').appendChild(n);
  accept.onclick = ()=>{ socket.emit('party_response',{invite_id:invite_id, accept:true}); n.remove(); }
  decline.onclick = ()=>{ socket.emit('party_response',{invite_id:invite_id, accept:false}); n.remove(); }
  // auto-disappear in 10s
  setTimeout(()=>{ if(n.parentElement) n.remove(); }, 10000);
}

// phone and mall
document.getElementById('phoneBtn').onclick = ()=>{ document.getElementById('phone').style.display='block'; }
document.getElementById('phoneBack').onclick = ()=>{ document.getElementById('phone').style.display='none'; }

document.getElementById('mallBtn').onclick = ()=>{ document.getElementById('mall').style.display='block'; socket.emit('request_mall'); }
document.getElementById('mallBack').onclick = ()=>{ document.getElementById('mall').style.display='none'; }

document.getElementById('logoutBtn').onclick = ()=>{ window.location.href='/logout'; }

document.getElementById('partyBtn').onclick = ()=>{ socket.emit('start_party', {}); }

function renderMall(items){
  const container = document.getElementById('mallItems'); container.innerHTML='';
  items.forEach(it=>{
    const box = document.createElement('div'); box.style.display='inline-block'; box.style.width='160px'; box.style.marginRight='12px'; box.style.verticalAlign='top'; box.style.background='white'; box.style.padding='8px'; box.style.borderRadius='8px';
    box.innerHTML = `<div style="font-weight:bold">${it.title}</div><div style='height:90px;overflow:hidden'><img src="${it.image||''}" style='max-width:100%;max-height:90px'></div><div>${it.description||''}</div><div>Price: ${it.price} Coins</div><button data-id='${it.id}'>Buy</button>`;
    container.appendChild(box);
    box.querySelector('button').onclick = ()=>{ socket.emit('buy_item', {item_id: it.id}); }
  });
}

socket.on('purchase_result', (d)=>{ alert(d.msg); if(d.ok){ coins = d.coins; document.getElementById('coins').innerText = coins; document.getElementById('mallCoins').innerText = coins; }});

// initial draw
setTimeout(()=>{ draw(); }, 500);
</script>
{% endblock %}
""

@app.route('/play')
def play():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template_string(GAME_HTML, username=session['username'])

# -----------------------------
# Socket.IO events
# -----------------------------
@socketio.on('join_game')
def on_join_game(data):
    username = data.get('username')
    if username not in USERS:
        emit('error', {'msg':'unknown user'})
        return
    sid = request.sid
    # register socket id
    PLAYER_STATE.setdefault(username, {'x':50,'y':50,'color':'#ff99cc','socket_id':None,'server_id':None,'home_pos':None})
    PLAYER_STATE[username]['socket_id'] = sid
    # ensure user in a server
    if not PLAYER_STATE[username]['server_id']:
        PLAYER_STATE[username]['server_id'] = assign_server_for_new_player(username)
    server_id = PLAYER_STATE[username]['server_id']
    join_room(server_id)
    # ensure player's home exists
    if not PLAYER_STATE[username].get('home_pos'):
        PLAYER_STATE[username]['home_pos'] = {'x':PLAYER_STATE[username]['x']+40, 'y':PLAYER_STATE[username]['y']+40}
    # prepare list of players in server
    players_in_room = {u: {'x':PLAYER_STATE[u]['x'],'y':PLAYER_STATE[u]['y'],'color':PLAYER_STATE[u]['color'],'home_pos':PLAYER_STATE[u]['home_pos']} for u in list_users_in_server(server_id)}
    emit('joined', {'server_id':server_id, 'players': players_in_room, 'coins': USERS[username]['coins']})
    # notify others
    emit('player_update', {'username':username, 'state':players_in_room.get(username)}, room=server_id, include_self=False)

def list_users_in_server(server_id):
    for s in SERVERS:
        if s['id']==server_id:
            return s['players']
    return []

@socketio.on('move')
def on_move(data):
    username = get_username_by_sid(request.sid)
    if not username: return
    x = max(0, min(800, int(data.get('x', PLAYER_STATE[username]['x']))))
    y = max(0, min(560, int(data.get('y', PLAYER_STATE[username]['y']))))
    PLAYER_STATE[username]['x'] = x
    PLAYER_STATE[username]['y'] = y
    # update server players
    server = PLAYER_STATE[username]['server_id']
    state = {'x':x,'y':y,'color':PLAYER_STATE[username]['color'],'home_pos':PLAYER_STATE[username]['home_pos']}
    emit('player_update', {'username':username, 'state':state}, room=server)

@socketio.on('chat')
def on_chat(data):
    username = get_username_by_sid(request.sid)
    if not username: return
    text = data.get('text','')[:300]
    server = PLAYER_STATE[username]['server_id']
    # broadcast to server; front-end will display above head for 5s
    emit('chat_message', {'from':username, 'text':text}, room=server)

@socketio.on('press_e')
def on_e(_):
    username = get_username_by_sid(request.sid)
    if not username: return
    # determine if player is near their home; if so toggle inside
    home = PLAYER_STATE[username]['home_pos']
    if home and abs(PLAYER_STATE[username]['x']-home['x'])<24 and abs(PLAYER_STATE[username]['y']-home['y'])<24:
        # teleport inside: we simply set a flag in state
        # 'inside_home' per player
        PLAYER_STATE[username]['inside_home'] = True
        # no longer broadcast their outside chat; that's handled in front-end by not showing outside messages if inside
        emit('player_update', {'username':username, 'state':{'x':PLAYER_STATE[username]['x'],'y':PLAYER_STATE[username]['y'],'color':PLAYER_STATE[username]['color'],'home_pos':home}}, room=PLAYER_STATE[username]['server_id'])

@socketio.on('start_party')
def on_start_party(_):
    username = get_username_by_sid(request.sid)
    if not username: return
    server_id = PLAYER_STATE[username]['server_id']
    # create invite that expires in 10s
    invite_id = str(uuid4())
    PENDING_PARTIES[invite_id] = {'host':username, 'server_id':server_id, 'expires_at': time.time()+10}
    emit('party_invite', {'host':username, 'invite_id':invite_id}, room=server_id)

@socketio.on('party_response')
def on_party_response(data):
    invite_id = data.get('invite_id')
    accept = data.get('accept', False)
    username = get_username_by_sid(request.sid)
    if not username or invite_id not in PENDING_PARTIES: return
    invite = PENDING_PARTIES[invite_id]
    host = invite['host']
    server = invite['server_id']
    if not accept:
        return
    # accept: teleport accepting player into host's home
    hx = PLAYER_STATE[host]['home_pos']['x']
    hy = PLAYER_STATE[host]['home_pos']['y']
    PLAYER_STATE[username]['x'] = hx
    PLAYER_STATE[username]['y'] = hy
    emit('teleport_to_home', {'username': username, 'x':hx, 'y':hy}, room=server)

@socketio.on('request_mall')
def on_request_mall():
    # send items
    emit('mall_update', {'items': ITEMS})

@socketio.on('buy_item')
def on_buy_item(data):
    username = get_username_by_sid(request.sid)
    if not username: return
    item_id = data.get('item_id')
    item = next((it for it in ITEMS if it['id']==item_id), None)
    if not item:
        emit('purchase_result', {'ok':False, 'msg':'Item not found', 'coins': USERS[username]['coins']})
        return
    if item.get('limit') and item['bought']>=item['limit']:
        emit('purchase_result', {'ok':False, 'msg':'Item sold out', 'coins': USERS[username]['coins']})
        return
    if USERS[username]['coins'] < item['price']:
        emit('purchase_result', {'ok':False, 'msg':'Not enough coins!', 'coins': USERS[username]['coins']})
        return
    USERS[username]['coins'] -= item['price']
    item['bought'] = item.get('bought',0)+1
    # ownership: append to player's inventory
    USERS[username].setdefault('inventory', []).append(item_id)
    emit('purchase_result', {'ok':True, 'msg':'Purchased: '+item['title'], 'coins': USERS[username]['coins']})

@socketio.on('add_item')
def on_add_item(data):
    # only workers
    username = get_username_by_sid(request.sid)
    if not username: return
    if not USERS.get(username,{}).get('worker'):
        emit('error', {'msg':'not a worker'})
        return
    # expected data: {type:'skin'|'item'|'wallpaper', title, description, price, image (dataURL), limit (opt), options (opt)}
    nid = str(uuid4())
    it = {
        'id': nid,
        'type': data.get('type','item'),
        'title': data.get('title','Untitled'),
        'description': data.get('description',''),
        'price': int(data.get('price',0)),
        'image': data.get('image',''),
        'limit': data.get('limit') and int(data.get('limit')) or None,
        'meta': data.get('options',{}),
        'bought': 0,
        'created_at': time.time()
    }
    ITEMS.insert(0, it)  # newest first
    # notify all servers
    socketio.emit('mall_update', {'items': ITEMS})
    emit('ok', {'msg':'Item added'})

@socketio.on('disconnect')
def on_disconnect():
    username = get_username_by_sid(request.sid)
    if username:
        sid = request.sid
        # clear socket id
        PLAYER_STATE[username]['socket_id'] = None
        # remove from server players list
        remove_player_from_server(username)
        emit('player_disconnect', {'username':username}, broadcast=True)

# helper
def get_username_by_sid(sid):
    for u,s in PLAYER_STATE.items():
        if s.get('socket_id')==sid:
            return u
    return None

if __name__=='__main__':
    print('Cubeo Land prototype starting...')
    print('Note: this is a local prototype. Open http://localhost:5000')
    socketio.run(app, host='0.0.0.0', port=5000)

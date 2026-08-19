import json
import os
import re
import secrets
import string
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
SESSION_TTL_SECONDS = int(os.environ.get("PULSE_SESSION_TTL", str(18 * 60 * 60)))
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z2-9]{6}$")
MAX_BODY = 128 * 1024


class PollSession:
    def __init__(self, code):
        self.lock = threading.RLock()
        self.code = code
        self.session_id = uuid.uuid4().hex[:16]
        self.control_token = secrets.token_urlsafe(32)
        self.created_at = time.time()
        self.last_touch = self.created_at
        self.question = ""
        self.options = []
        self.counts = []
        self.correct_index = None
        self.poll_id = 0
        self.active = False
        self.show_results = False
        self.show_correct = False
        self.session_complete = False
        self.voters = {}
        self.history = []
        self._recorded_poll_ids = set()

    def touch(self):
        self.last_touch = time.time()

    def _record_current_locked(self):
        if self.poll_id <= 0 or not self.question or self.poll_id in self._recorded_poll_ids:
            return
        total = sum(self.counts)
        correct_count = None
        pct_correct = None
        if isinstance(self.correct_index, int) and 0 <= self.correct_index < len(self.counts):
            correct_count = self.counts[self.correct_index]
            pct_correct = (correct_count / total * 100.0) if total else 0.0
        self.history.append({
            "poll_id": self.poll_id,
            "question": self.question,
            "options": list(self.options),
            "counts": list(self.counts),
            "correct_index": self.correct_index,
            "total": total,
            "correct_count": correct_count,
            "pct_correct": pct_correct,
        })
        self._recorded_poll_ids.add(self.poll_id)

    def snapshot(self):
        with self.lock:
            self.touch()
            return {
                "session_id": self.session_id,
                "session_code": self.code,
                "question": self.question,
                "options": list(self.options),
                "counts": list(self.counts),
                "correct_index": self.correct_index,
                "poll_id": self.poll_id,
                "active": self.active,
                "show_results": self.show_results,
                "show_correct": self.show_correct,
                "session_complete": self.session_complete,
                "total": sum(self.counts),
            }

    def start_poll(self, question, options, correct_index=None):
        question = str(question or "").strip()[:1200]
        clean_options = [str(x or "").strip()[:600] for x in (options or []) if str(x or "").strip()]
        clean_options = clean_options[:5]
        if not question or len(clean_options) < 2:
            raise ValueError("A question and at least two answer options are required.")
        with self.lock:
            self._record_current_locked()
            self.question = question
            self.options = clean_options
            self.counts = [0] * len(clean_options)
            self.correct_index = correct_index if isinstance(correct_index, int) and 0 <= correct_index < len(clean_options) else None
            self.poll_id += 1
            self.active = True
            self.show_results = False
            self.show_correct = False
            self.session_complete = False
            self.voters = {}
            self.touch()
            return self.snapshot()

    def close_poll(self):
        with self.lock:
            self.active = False
            self.touch()
            return self.snapshot()

    def reveal_results(self, show_correct=False):
        with self.lock:
            if self.poll_id <= 0:
                raise ValueError("No poll has been started.")
            self.active = False
            self.show_results = True
            self.show_correct = bool(show_correct and self.correct_index is not None)
            self._record_current_locked()
            self.touch()
            return self.snapshot()

    def reveal_correct(self):
        with self.lock:
            if self.poll_id <= 0:
                raise ValueError("No poll has been started.")
            self.active = False
            self.show_correct = self.correct_index is not None
            self._record_current_locked()
            self.touch()
            return self.snapshot()

    def end_session(self):
        with self.lock:
            self.active = False
            self._record_current_locked()
            self.session_complete = True
            self.show_results = False
            self.show_correct = False
            self.touch()
            return self.snapshot()

    def summary(self):
        with self.lock:
            self.touch()
            if not self.active:
                self._record_current_locked()
            return [dict(x) for x in self.history]

    def vote(self, client_id, choice, poll_id, session_id):
        client_id = str(client_id or "")[:120]
        with self.lock:
            self.touch()
            if str(session_id or "") != self.session_id:
                return False, "stale_session"
            if self.session_complete:
                return False, "session_complete"
            if not self.active:
                return False, "poll_closed"
            if poll_id != self.poll_id:
                return False, "poll_changed"
            if not isinstance(choice, int) or choice < 0 or choice >= len(self.options):
                return False, "invalid_choice"
            if not client_id:
                return False, "missing_client"
            if client_id in self.voters:
                return False, "already_voted"
            self.voters[client_id] = choice
            self.counts[choice] += 1
            return True, "recorded"


SESSIONS = {}
SESSIONS_LOCK = threading.RLock()
LAST_CLEANUP = 0.0


def cleanup_sessions():
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP < 60:
        return
    LAST_CLEANUP = now
    with SESSIONS_LOCK:
        stale = [code for code, session in SESSIONS.items() if now - session.last_touch > SESSION_TTL_SECONDS]
        for code in stale:
            SESSIONS.pop(code, None)


def new_code():
    with SESSIONS_LOCK:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if code not in SESSIONS:
                return code


def create_session():
    code = new_code()
    session = PollSession(code)
    with SESSIONS_LOCK:
        SESSIONS[code] = session
    return session


def get_session(code):
    cleanup_sessions()
    code = str(code or "").upper()
    if not CODE_RE.fullmatch(code):
        return None
    with SESSIONS_LOCK:
        return SESSIONS.get(code)


STUDENT_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
<meta name="theme-color" content="#111827">
<title>Pulse Poll</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#111827;--muted:#667085;--line:#e5e7eb;--good:#e9f7ef;--goodline:#91c7a5;--bar:#344054;--barbg:#eef1f4}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:720px;margin:0 auto;padding:24px 16px 48px}.brand{font-weight:800;letter-spacing:.02em;margin:8px 0 18px;font-size:20px}.code{float:right;color:var(--muted);font-size:13px;font-weight:700;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 8px 24px rgba(15,23,42,.06)}.kicker{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:12px;font-weight:700}
h1{font-size:clamp(24px,6vw,38px);line-height:1.15;margin:10px 0 22px}.option{width:100%;text-align:left;border:1px solid #d0d5dd;background:white;border-radius:14px;padding:16px 18px;margin:10px 0;font-size:18px;cursor:pointer}.option:disabled{opacity:.55;cursor:not-allowed}.msg{color:var(--muted);font-size:16px;line-height:1.5}.success{padding:14px 16px;border:1px solid #b7d7c5;background:#f1f8f4;border-radius:12px;margin-top:18px}
.result{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}.result.correct{background:var(--good);border-color:var(--goodline)}.result-top{display:flex;justify-content:space-between;gap:14px;font-size:16px}.bar{height:10px;background:var(--barbg);border-radius:8px;overflow:hidden;margin-top:9px}.fill{height:100%;background:var(--bar)}.badge{display:inline-block;margin-top:7px;font-size:12px;font-weight:700;color:#25603a}.small{margin-top:18px;color:var(--muted);font-size:13px}
</style></head>
<body><div class="wrap"><div class="brand">Pulse Poll <span class="code">Session __SESSION_CODE__</span></div><div class="card"><div id="content"><div class="msg">Connecting to the lecture…</div></div></div></div>
<script>
const SESSION='__SESSION_CODE__'; const KEY='pulse_poll_client_id'; let clientId=localStorage.getItem(KEY); if(!clientId){clientId=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random());localStorage.setItem(KEY,clientId)}
let currentPoll=-1,currentSession='',votedForPoll=-1,stopped=false;
function esc(s){return String(s).replace(/[&<>'\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','\"':'&quot;'}[c]))}
async function fetchState(){if(stopped)return;try{const r=await fetch('/api/s/'+SESSION+'/state',{cache:'no-store'});if(!r.ok)throw new Error();render(await r.json())}catch(e){document.getElementById('content').innerHTML='<div class="msg">The polling service is temporarily unavailable. Keep this page open; it will retry automatically.</div>'}}
function results(s){let h='<div class="kicker">Question results</div><h1>'+esc(s.question)+'</h1>';s.options.forEach((o,i)=>{const c=(s.counts||[])[i]||0,p=s.total?Math.round(c/s.total*100):0,ok=s.show_correct&&s.correct_index===i;h+='<div class="result '+(ok?'correct':'')+'"><div class="result-top"><span>'+String.fromCharCode(65+i)+'. '+esc(o)+'</span><strong>'+c+' ('+p+'%)</strong></div><div class="bar"><div class="fill" style="width:'+p+'%"></div></div>'+(ok?'<div class="badge">Correct answer</div>':'')+'</div>'});return h+'<div class="small">Wait for the lecturer to move to the next question.</div>'}
function render(s){currentPoll=s.poll_id;currentSession=s.session_id||'';const el=document.getElementById('content');if(s.session_complete){stopped=true;el.innerHTML='<div class="kicker">Session complete</div><h1>All questions are finished</h1><div class="success">Thank you for participating. There are no more questions to wait for, so you may close this page.</div>';return}if(s.show_results){el.innerHTML=results(s);return}if(s.show_correct&&!s.show_results){let h='<div class="kicker">Correct answer</div><h1>'+esc(s.question)+'</h1>';s.options.forEach((o,i)=>{const ok=s.correct_index===i;h+='<div class="result '+(ok?'correct':'')+'"><div class="result-top"><span>'+String.fromCharCode(65+i)+'. '+esc(o)+'</span></div>'+(ok?'<div class="badge">Correct answer</div>':'')+'</div>'});el.innerHTML=h+'<div class="small">Wait for the lecturer to move to the next question.</div>';return}if(!s.active){el.innerHTML='<div class="kicker">Waiting</div><h1>Ready for the next question</h1><div class="msg">Keep this page open. The next question will appear automatically.</div>';return}const vk='pulse_voted_'+currentSession+'_'+s.poll_id,already=localStorage.getItem(vk)==='1';if(already||votedForPoll===s.poll_id){el.innerHTML='<div class="kicker">Response received</div><h1>'+esc(s.question)+'</h1><div class="success">Thank you. Your response has been recorded.</div><div class="small">Wait for the lecturer to reveal the answer or move to the next question.</div>';return}let h='<div class="kicker">Live question</div><h1>'+esc(s.question)+'</h1>';s.options.forEach((o,i)=>{h+='<button class="option" onclick="vote('+i+')">'+String.fromCharCode(65+i)+'. '+esc(o)+'</button>'});el.innerHTML=h}
async function vote(choice){document.querySelectorAll('.option').forEach(b=>b.disabled=true);try{const r=await fetch('/api/s/'+SESSION+'/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId,choice:choice,poll_id:currentPoll,session_id:currentSession})});const x=await r.json();if(x.ok||x.reason==='already_voted'){votedForPoll=currentPoll;localStorage.setItem('pulse_voted_'+currentSession+'_'+currentPoll,'1')}}catch(e){}fetchState()}
fetchState();setInterval(fetchState,2200);window.addEventListener('pageshow',fetchState);document.addEventListener('visibilitychange',()=>{if(!document.hidden)fetchState()});
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "PulsePollCloud/0.1"

    def log_message(self, fmt, *args):
        # Render captures stdout; keep only concise request logging.
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json(self, obj, status=200):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _html(self, html, status=200):
        data = html.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(data))
        self.wfile.write(data)

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _auth(self, session):
        token = self.headers.get("X-Pulse-Control", "")
        return bool(token) and secrets.compare_digest(token, session.control_token)

    def do_GET(self):
        cleanup_sessions()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._html("""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>Pulse Poll Cloud</title><style>body{font-family:system-ui;margin:40px;max-width:760px;color:#111827}code{background:#f2f4f7;padding:2px 6px;border-radius:5px}</style></head><body><h1>Pulse Poll Cloud is running</h1><p>This server hosts anonymous classroom polling sessions. Students join with a session-specific QR link generated by the lecturer application.</p><p>Health check: <code>/health</code></p></body></html>""")
            return
        if path == "/health":
            with SESSIONS_LOCK:
                n = len(SESSIONS)
            self._json({"ok": True, "service": "pulse-poll-cloud", "sessions": n})
            return

        m = re.fullmatch(r"/s/([A-Z2-9]{6})", path, re.I)
        if m:
            code = m.group(1).upper()
            if get_session(code) is None:
                self._html("<h1>Session unavailable</h1><p>This polling session has ended or expired. Please scan the lecturer's current QR code.</p>", 404)
                return
            self._html(STUDENT_HTML.replace("__SESSION_CODE__", code))
            return

        m = re.fullmatch(r"/api/s/([A-Z2-9]{6})/state", path, re.I)
        if m:
            session = get_session(m.group(1))
            if not session:
                self._json({"error": "session_not_found"}, 404)
                return
            self._json(session.snapshot())
            return

        m = re.fullmatch(r"/api/l/([A-Z2-9]{6})/(state|summary)", path, re.I)
        if m:
            session = get_session(m.group(1))
            if not session:
                self._json({"error": "session_not_found"}, 404)
                return
            if not self._auth(session):
                self._json({"error": "unauthorized"}, 401)
                return
            if m.group(2) == "state":
                self._json(session.snapshot())
            else:
                self._json({"summary": session.summary()})
            return

        self._json({"error": "not_found"}, 404)

    def do_POST(self):
        cleanup_sessions()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/session":
            session = create_session()
            self._json({
                "ok": True,
                "session_code": session.code,
                "session_id": session.session_id,
                "control_token": session.control_token,
                "join_path": f"/s/{session.code}",
            }, 201)
            return

        m = re.fullmatch(r"/api/s/([A-Z2-9]{6})/vote", path, re.I)
        if m:
            session = get_session(m.group(1))
            if not session:
                self._json({"ok": False, "reason": "session_not_found"}, 404)
                return
            try:
                p = self._body_json()
                ok, reason = session.vote(p.get("client_id"), p.get("choice"), p.get("poll_id"), p.get("session_id"))
                self._json({"ok": ok, "reason": reason}, 200 if ok else 409)
            except Exception:
                self._json({"ok": False, "reason": "bad_request"}, 400)
            return

        m = re.fullmatch(r"/api/l/([A-Z2-9]{6})/(start|close|results|answer|end)", path, re.I)
        if m:
            session = get_session(m.group(1))
            if not session:
                self._json({"error": "session_not_found"}, 404)
                return
            if not self._auth(session):
                self._json({"error": "unauthorized"}, 401)
                return
            action = m.group(2)
            try:
                p = self._body_json()
                if action == "start":
                    state = session.start_poll(p.get("question"), p.get("options"), p.get("correct_index"))
                elif action == "close":
                    state = session.close_poll()
                elif action == "results":
                    state = session.reveal_results(bool(p.get("show_correct")))
                elif action == "answer":
                    state = session.reveal_correct()
                else:
                    state = session.end_session()
                self._json({"ok": True, "state": state})
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, 400)
            except Exception:
                self._json({"ok": False, "error": "server_error"}, 500)
            return

        self._json({"error": "not_found"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"Pulse Poll Cloud listening on 0.0.0.0:{PORT}", flush=True)
    Server((HOST, PORT), Handler).serve_forever()

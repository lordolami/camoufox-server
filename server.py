"""
Camoufox REST server — single shared browser, one session at a time.
Each session gets a fresh page in a fresh context. The browser process
stays alive across sessions to avoid the ~300MB Firefox startup cost.

Railway free tier: 512MB RAM. One Firefox + one active page fits.
"""
import os
import re
import secrets
import threading
import time
import traceback

from flask import Flask, request, jsonify

try:
    from camoufox.sync_api import Camoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False
    print("[camoufox] WARNING: camoufox not installed")

app = Flask(__name__)

# ── Single shared browser instance ────────────────────────────────────────────
_browser      = None
_camoufox_ctx = None          # the Camoufox context manager
_browser_lock = threading.Lock()
MAX_SESSIONS  = 2             # max concurrent sessions (memory guard)

# ── Sessions: sid -> {context, page, intercepted, intercept_lock, created} ────
sessions      = {}
sessions_lock = threading.Lock()

# ── Ad network patterns ────────────────────────────────────────────────────────
AD_IMPRESSION_RE = re.compile(
    r'googlesyndication\.com|doubleclick\.net|googletagservices\.com|'
    r'securepubads\.g\.doubleclick\.net|pagead2\.googlesyndication\.com|'
    r'rubiconproject\.com|pubmatic\.com|openx\.net|indexww\.com|'
    r'appnexus\.com|adnxs\.com|criteo\.com|sharethrough\.com|triplelift\.com|'
    r'teads\.tv|smartadserver\.com|33across\.com|emxdgt\.com|sovrn\.com|'
    r'lijit\.com|advertising\.com|amazon-adsystem\.com|aps\.amazon\.com|'
    r'adsrvr\.org|moatads\.com|doubleverify\.com|iasds01\.com|'
    r'integral-platform\.com|gumgum\.com|media\.net|yieldmo\.com|'
    r'kargo\.com|districtm\.io|rhythmone\.com|spotx\.tv|springserve\.com|'
    r'smaato\.net|contextweb\.com|onetag\.net|richaudience\.com|'
    r'undertone\.com|bid\.g\.doubleclick\.net|'
    r'/beacon[\?/]|/impression[\?/]|/track/impression|/ad/view|'
    r'/pixel[\?/]|/imp[\?.]|/i\.gif'
)
AD_CLICK_RE = re.compile(
    r'googleads\.g\.doubleclick\.net/aclk|ad\.doubleclick\.net/clk|'
    r'/click[\?/]|/track/click|/clk[\?/]|/ad/click'
)


def extract_network(url):
    if re.search(r'doubleclick|googlesyndication|googletagservices|pagead|securepubads', url): return 'GAM'
    if 'rubiconproject' in url: return 'Rubicon'
    if 'pubmatic'       in url: return 'PubMatic'
    if 'openx'          in url: return 'OpenX'
    if re.search(r'appnexus|adnxs', url): return 'AppNexus'
    if 'criteo'         in url: return 'Criteo'
    if 'amazon-adsystem' in url: return 'Amazon TAM'
    if 'moatads'        in url: return 'MOAT'
    if 'doubleverify'   in url: return 'DoubleVerify'
    if re.search(r'iasds|integral-platform', url): return 'IAS'
    if 'indexww'        in url: return 'Index Exchange'
    if 'sharethrough'   in url: return 'Sharethrough'
    if 'triplelift'     in url: return 'TripleLift'
    if 'adsrvr'         in url: return 'TheTradeDesk'
    if re.search(r'sovrn|lijit', url): return 'Sovrn'
    if '33across'       in url: return '33Across'
    if 'spotx'          in url: return 'SpotX'
    if 'teads'          in url: return 'Teads'
    return 'Unknown'


def get_browser():
    """Return the shared browser, launching it if needed."""
    global _browser, _camoufox_ctx
    with _browser_lock:
        if _browser is not None:
            try:
                # Quick liveness check
                _ = _browser.is_connected()
                return _browser
            except Exception:
                _browser = None
                _camoufox_ctx = None

        if not CAMOUFOX_AVAILABLE:
            return None

        print('[camoufox] Launching shared browser...')
        _camoufox_ctx = Camoufox(headless=True, geoip=True, os='windows')
        _browser = _camoufox_ctx.__enter__()
        print('[camoufox] Browser ready')
        return _browser


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    with sessions_lock:
        active = len(sessions)
    return jsonify({
        'ok': True,
        'camoufox_available': CAMOUFOX_AVAILABLE,
        'active_sessions': active,
        'max_sessions': MAX_SESSIONS,
    })


@app.route('/session/create', methods=['POST'])
def create_session():
    if not CAMOUFOX_AVAILABLE:
        return jsonify({'error': 'camoufox not available'}), 503

    with sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            return jsonify({'error': 'server busy', 'active': len(sessions)}), 503

    body     = request.json or {}
    proxy    = body.get('proxy')  # {'server': 'http://ip:port'}

    try:
        browser = get_browser()
        if browser is None:
            return jsonify({'error': 'browser not available'}), 503

        ctx_kwargs = {}
        if proxy:
            ctx_kwargs['proxy'] = proxy

        context = browser.new_context(**ctx_kwargs)
        page    = context.new_page()

        intercepted    = []
        intercept_lock = threading.Lock()

        def on_request(req):
            url  = req.url
            kind = None
            if AD_IMPRESSION_RE.search(url):   kind = 'impression'
            elif AD_CLICK_RE.search(url):       kind = 'click'
            if kind:
                with intercept_lock:
                    intercepted.append({
                        'url': url, 'type': kind,
                        'network': extract_network(url),
                        'status': None, 'caught': False,
                        'time': time.time(),
                    })

        def on_response(resp):
            url    = resp.url
            status = resp.status
            caught = status in (400, 401, 403, 429, 503)
            with intercept_lock:
                for e in intercepted:
                    if e['url'] == url and e['status'] is None:
                        e['status'] = status
                        e['caught'] = caught
                        break

        page.on('request',  on_request)
        page.on('response', on_response)

        sid = secrets.token_hex(16)
        with sessions_lock:
            sessions[sid] = {
                'context': context, 'page': page,
                'intercepted': intercepted, 'intercept_lock': intercept_lock,
                'created': time.time(),
            }

        return jsonify({'session_id': sid, 'ok': True})

    except Exception as e:
        traceback.print_exc()
        # If browser died, reset so next call relaunches it
        global _browser, _camoufox_ctx
        _browser = None
        _camoufox_ctx = None
        return jsonify({'error': str(e)}), 500


@app.route('/session/<sid>/navigate', methods=['POST'])
def navigate(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404

    body     = request.json or {}
    url      = body.get('url', '')
    wait_for = body.get('wait_for', 'domcontentloaded')
    timeout  = body.get('timeout', 25000)

    try:
        page     = sess['page']
        response = page.goto(url, wait_until=wait_for, timeout=timeout)
        status   = response.status if response else 0
        page.wait_for_timeout(2500)
        try:
            page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        return jsonify({'ok': True, 'status': status, 'title': page.title(), 'url': page.url})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/scroll', methods=['POST'])
def scroll(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    body   = request.json or {}
    amount = body.get('amount', 300)
    try:
        sess['page'].evaluate(f'window.scrollBy({{top: {amount}, behavior: "smooth"}})')
        sess['page'].wait_for_timeout(350)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/scroll_to_ad', methods=['POST'])
def scroll_to_ad(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404

    AD_SELECTORS = [
        'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]',
        'div[id*="div-gpt-ad"]', 'ins.adsbygoogle', 'div[data-ad-unit]',
        'div[class*="ad-slot"]', 'div[id*="google_ads_iframe"]',
        '[data-google-query-id]', 'iframe[id*="aswift"]',
        'div[class*="ad-container"]', '[class*="advertisement"]',
    ]
    page = sess['page']
    try:
        for sel in AD_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.scroll_into_view_if_needed(timeout=2000)
                    page.wait_for_timeout(400)
                    box = el.bounding_box()
                    return jsonify({'ok': True, 'found': True, 'box': box})
            except Exception:
                continue
        page.evaluate('window.scrollTo({top: document.body.scrollHeight * 0.3, behavior: "smooth"})')
        page.wait_for_timeout(500)
        return jsonify({'ok': True, 'found': False})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/click_ad', methods=['POST'])
def click_ad(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404

    body = request.json or {}
    x    = body.get('x')
    y    = body.get('y')
    page = sess['page']

    try:
        if x is not None and y is not None:
            page.mouse.click(x, y)
            return jsonify({'ok': True, 'clicked': True})

        for sel in ['iframe[src*="doubleclick"]', 'ins.adsbygoogle',
                    'div[id*="div-gpt-ad"]', 'iframe[id*="aswift"]']:
            try:
                el = page.query_selector(sel)
                if el:
                    box = el.bounding_box()
                    if box and box['width'] > 30 and box['height'] > 30:
                        import random
                        page.mouse.click(
                            box['x'] + box['width']  * (0.3 + random.random() * 0.4),
                            box['y'] + box['height'] * (0.3 + random.random() * 0.4),
                        )
                        return jsonify({'ok': True, 'clicked': True})
            except Exception:
                continue
        return jsonify({'ok': True, 'clicked': False})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/mouse_move', methods=['POST'])
def mouse_move(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    body = request.json or {}
    try:
        sess['page'].mouse.move(body.get('x', 400), body.get('y', 300))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/get_intercepted', methods=['GET'])
def get_intercepted(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    with sess['intercept_lock']:
        data = list(sess['intercepted'])
    return jsonify({'ok': True, 'intercepted': data})


@app.route('/session/<sid>/evaluate', methods=['POST'])
def evaluate(sid):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    body = request.json or {}
    try:
        result = sess['page'].evaluate(body.get('script', 'null'))
        return jsonify({'ok': True, 'result': result})
    except Exception as e:
        return jsonify({'error': str(e), 'ok': False})


@app.route('/session/<sid>/close', methods=['POST'])
def close_session(sid):
    with sessions_lock:
        sess = sessions.pop(sid, None)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    try:
        sess['page'].close()
        sess['context'].close()
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/sessions')
def list_sessions():
    with sessions_lock:
        return jsonify({'count': len(sessions), 'ids': list(sessions.keys())})


# ── Session reaper — kill sessions older than 8 min ──────────────────────────
def reaper():
    while True:
        time.sleep(60)
        cutoff = time.time() - 480  # 8 min
        with sessions_lock:
            expired = [sid for sid, s in sessions.items() if s['created'] < cutoff]
        for sid in expired:
            with sessions_lock:
                sess = sessions.pop(sid, None)
            if sess:
                try:
                    sess['page'].close()
                    sess['context'].close()
                except Exception:
                    pass
                print(f'[camoufox] Reaped expired session {sid}')


threading.Thread(target=reaper, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('CAMOUFOX_PORT', 8080))
    print(f'[camoufox] Starting on port {port} — camoufox_available={CAMOUFOX_AVAILABLE}')
    # Browser launches lazily on first /session/create to avoid OOM during startup
    app.run(host='0.0.0.0', port=port, threaded=True)

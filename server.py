"""
Camoufox REST server — wraps Camoufox (Firefox fork with C++ fingerprint patches)
Intercepts all ad network requests during sessions and returns them to Node.js.

Start: python backend/camoufox/server.py
Port:  7331 (override with CAMOUFOX_PORT env var)
"""
import json
import os
import re
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

# sessions: sid -> { cf, browser, context, page, requests, created }
sessions = {}
sessions_lock = threading.Lock()

# ── Ad network patterns (same as browserEngine.js) ────────────────────────────
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
    if 'pubmatic' in url: return 'PubMatic'
    if 'openx' in url: return 'OpenX'
    if re.search(r'appnexus|adnxs', url): return 'AppNexus'
    if 'criteo' in url: return 'Criteo'
    if 'amazon-adsystem' in url: return 'Amazon TAM'
    if 'moatads' in url: return 'MOAT'
    if 'doubleverify' in url: return 'DoubleVerify'
    if re.search(r'iasds|integral-platform', url): return 'IAS'
    if 'indexww' in url: return 'Index Exchange'
    if 'sharethrough' in url: return 'Sharethrough'
    if 'triplelift' in url: return 'TripleLift'
    if 'adsrvr' in url: return 'TheTradeDesk'
    if re.search(r'sovrn|lijit', url): return 'Sovrn'
    if '33across' in url: return '33Across'
    if 'spotx' in url: return 'SpotX'
    if 'teads' in url: return 'Teads'
    return 'Unknown'

def make_sid():
    import secrets
    return secrets.token_hex(16)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'camoufox_available': CAMOUFOX_AVAILABLE})

@app.route('/session/create', methods=['POST'])
def create_session():
    if not CAMOUFOX_AVAILABLE:
        return jsonify({'error': 'camoufox not available'}), 503

    body = request.json or {}
    locale   = body.get('locale', 'en-US')
    os_name  = body.get('os', 'windows')
    geoip    = body.get('geoip', True)
    proxy    = body.get('proxy')  # {'server': 'http://ip:port'}

    try:
        kwargs = { 'geoip': geoip, 'os': os_name, 'locale': locale, 'headless': True }
        if proxy:
            kwargs['proxy'] = proxy

        cf      = Camoufox(**kwargs)
        browser = cf.__enter__()
        context = browser.new_context()
        page    = context.new_page()

        # Storage for intercepted ad requests
        intercepted = []
        intercept_lock = threading.Lock()

        def on_request(req):
            url = req.url
            kind = None
            if AD_IMPRESSION_RE.search(url):
                kind = 'impression'
            elif AD_CLICK_RE.search(url):
                kind = 'click'
            if kind:
                with intercept_lock:
                    intercepted.append({
                        'url':     url,
                        'type':    kind,
                        'network': extract_network(url),
                        'status':  None,
                        'caught':  False,
                        'time':    time.time(),
                    })

        def on_response(resp):
            url    = resp.url
            status = resp.status
            caught = status in (400, 401, 403, 429, 503)
            with intercept_lock:
                for entry in intercepted:
                    if entry['url'] == url and entry['status'] is None:
                        entry['status'] = status
                        entry['caught'] = caught
                        break

        page.on('request',  on_request)
        page.on('response', on_response)

        sid = make_sid()
        with sessions_lock:
            sessions[sid] = {
                'cf': cf, 'browser': browser, 'context': context,
                'page': page, 'intercepted': intercepted,
                'intercept_lock': intercept_lock, 'created': time.time()
            }

        return jsonify({'session_id': sid, 'ok': True})

    except Exception as e:
        traceback.print_exc()
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

        # Extra wait for ad tags to fire
        page.wait_for_timeout(2500)

        # Try networkidle
        try:
            page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass

        # Another short wait for Prebid
        page.wait_for_timeout(1000)

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
        sess['page'].wait_for_timeout(400)
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
                    page.wait_for_timeout(500)
                    box = el.bounding_box()
                    return jsonify({'ok': True, 'found': True, 'box': box})
            except Exception:
                continue
        # Fallback scroll
        page.evaluate('window.scrollTo({top: document.body.scrollHeight * 0.3, behavior: "smooth"})')
        page.wait_for_timeout(600)
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

        AD_SELECTORS = [
            'iframe[src*="doubleclick"]', 'ins.adsbygoogle',
            'div[id*="div-gpt-ad"]', 'iframe[id*="aswift"]',
        ]
        for sel in AD_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    box = el.bounding_box()
                    if box and box['width'] > 30 and box['height'] > 30:
                        import random
                        cx = box['x'] + box['width']  * (0.3 + random.random() * 0.4)
                        cy = box['y'] + box['height'] * (0.3 + random.random() * 0.4)
                        page.mouse.click(cx, cy)
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
    x    = body.get('x', 400)
    y    = body.get('y', 300)
    try:
        sess['page'].mouse.move(x, y)
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

    body   = request.json or {}
    script = body.get('script', 'null')
    try:
        result = sess['page'].evaluate(script)
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
        sess['cf'].__exit__(None, None, None)
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/sessions')
def list_sessions():
    with sessions_lock:
        return jsonify({'count': len(sessions), 'ids': list(sessions.keys())})

# ── Session reaper — kill sessions older than 10 min ─────────────────────────
def reaper():
    while True:
        time.sleep(60)
        cutoff = time.time() - 600
        with sessions_lock:
            expired = [sid for sid, s in sessions.items() if s['created'] < cutoff]
        for sid in expired:
            with sessions_lock:
                sess = sessions.pop(sid, None)
            if sess:
                try:
                    sess['page'].close()
                    sess['context'].close()
                    sess['cf'].__exit__(None, None, None)
                except Exception:
                    pass

threading.Thread(target=reaper, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('CAMOUFOX_PORT', 7331))
    print(f'[camoufox] Starting on port {port} — camoufox_available={CAMOUFOX_AVAILABLE}')
    app.run(host='0.0.0.0', port=port, threaded=True)

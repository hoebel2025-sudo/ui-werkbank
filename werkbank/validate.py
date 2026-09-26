"""Independent candidate smoke check. No provider calls and no external HTTP."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


def validate(html):
    body = html.encode('utf-8')
    result = {'ok': False, 'sha256': hashlib.sha256(body).hexdigest(), 'errors': [], 'external': [],
              'scope': 'Browserstart, JavaScript-Seitenfehler, externe Zugriffe; keine fachliche/UI-Abnahme.'}
    class Page(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/favicon.ico':
                self.send_response(204); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            result['errors'].append('Playwright fehlt in der Werkbank-Umgebung. Im Werkbank-Ordner "python3 werkbank.py setup" ausfuehren.')
            return result
        from urllib.parse import urlsplit
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
                def route(r):
                    u = urlsplit(r.request.url)
                    if u.hostname == '127.0.0.1' and u.port == server.server_port: r.continue_()
                    else: result['external'].append(r.request.url[:300]); r.abort()
                ctx.route('**/*', route)
                pg = ctx.new_page(); pg.on('pageerror', lambda e: result['errors'].append(str(e)))
                pg.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='load', timeout=15000)
                pg.wait_for_timeout(500)
                result['ok'] = bool(pg.locator('body').count()) and not result['errors'] and not result['external']
            finally: browser.close()
    except Exception as exc:
        result['errors'].append(str(exc)[:1000])
    finally:
        server.shutdown(); thread.join(timeout=3); server.server_close()
    return result

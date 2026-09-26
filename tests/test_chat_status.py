"""Typing with a connected terminal must not move or resize the chat controls."""
import pytest

from conftest import registered, api


@pytest.mark.parametrize('width', [380, 600])
def test_typing_preserves_chat_geometry_and_connection(server, width):
    from playwright.sync_api import sync_playwright
    instance, _, url = server
    client = registered(server, 'codex')
    api(server, 'bind', {'project': 'demo', 'session': client.data['id']})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 950})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url); page.wait_for_selector('#senden:enabled')
            page.wait_for_function("() => document.getElementById('status').textContent.includes('codex-test')")
            page.evaluate("w=>{const c=document.getElementById('chat');c.style.width=w+'px';c.style.left='20px';c.style.top='100px';}", width)
            page.locator('#text').focus()
            page.wait_for_timeout(250)
            page.evaluate("""() => {
                window.typingSamples=[];window.sampleTyping=true;
                function sample(){
                    if(!window.sampleTyping)return;
                    const head=document.getElementById('ckopf').getBoundingClientRect();
                    const input=document.getElementById('text').getBoundingClientRect();
                    const chat=document.getElementById('chat').getBoundingClientRect();
                    window.typingSamples.push({head:head.height,input:input.y,width:chat.width,height:chat.height,status:document.getElementById('status').textContent});
                    requestAnimationFrame(sample);
                }requestAnimationFrame(sample);
            }""")
            text = 'Snapshot SC1 lesen'
            page.locator('#text').press_sequentially(text, delay=240)
            page.wait_for_timeout(700)
            samples = page.evaluate('() => {window.sampleTyping=false;return window.typingSamples;}')
            spans = {k: round(max(s[k] for s in samples) - min(s[k] for s in samples), 2) for k in ('head', 'input', 'width', 'height')}
            assert all(v < 1 for v in spans.values()), spans
            assert all('codex-test' in s['status'] for s in samples)
            assert page.locator('#text').input_value() == text
            assert instance.store.state()['documents']['ui/entwurf']['value']['text'] == text
            # A service outage still has to be visible and retain the unsaved input.
            page.route('**/api/**', lambda route: route.abort())
            page.locator('#text').press_sequentially('!')
            page.wait_for_function("() => document.getElementById('saveStatus').textContent.includes('Ungespeichert')")
            assert page.locator('#text').input_value() == text + '!'
            assert abs(page.locator('#ckopf').bounding_box()['height'] - samples[-1]['head']) < 1
            page.unroute('**/api/**')
            page.wait_for_function("() => document.getElementById('saveStatus').textContent==='Gespeichert'")
            assert instance.store.state()['documents']['ui/entwurf']['value']['text'] == text + '!'
            assert not errors, errors
        finally: browser.close()

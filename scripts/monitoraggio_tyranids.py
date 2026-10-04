"""Rileva cambiamenti nei documenti GW; non modifica manifest o database.
Gli eventi sono documenti da verificare, mai modifiche regolistiche confermate.
La prima lettura riuscita di ciascuna pagina stabilisce il riferimento.
"""
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip()


def edition_evidence(text):
    # Un filename, l'anno o il numero di versione NON dimostrano l'edizione.
    found = set()
    for n in (10, 11):
        if re.search(rf'\b{n}(?:th|ª|a|°)?\s*(?:edition|edizione)\b', text, re.I):
            found.add(n)
    return next(iter(found)) if len(found) == 1 else None


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            fields = dict(attrs)
            self.current = [fields.get('href', ''), fields.get('title', '')]

    def handle_data(self, data):
        if self.current:
            self.current[1] += ' ' + data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            self.items.append(self.current)
            self.current = None


def discover(html, page, cfg):
    if html.lstrip().startswith('{'):
        payload = json.loads(html)
        if payload.get('totalPages', 1) > 1:
            raise ValueError('Elenco GW paginato: controllo incompleto')
        pairs = []
        for hit in payload.get('hits', []):
            entry = hit.get('id', {})
            name = entry.get('title', hit.get('title', ''))
            file = entry.get('file', '')
            if file:
                pairs.append((urljoin('https://assets.warhammer-community.com/', file), name))
    else:
        pairs = []
    parser = Links()
    parser.feed(html)
    # Supporta anche URL PDF inseriti nei dati JSON della pagina.
    raw = unescape(html).replace('\\/', '/').replace('\\u0026', '&')
    pairs += list(parser.items)
    pairs += [(x, '') for x in re.findall(r'https://assets\.warhammer-community\.com/[^\s"<>\\]+?\.pdf(?:\?[^\s"<>\\]*)?', raw)]
    result = {}
    for href, label in pairs:
        url = urljoin(page, href)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in cfg['allowed_hosts']:
            continue
        if not parsed.path.lower().endswith('.pdf'):
            continue
        search = unescape(label + ' ' + parsed.path).lower().replace('_', ' ').replace('-', ' ')
        if any(term in search for term in cfg['document_terms']):
            result[url] = normalized(label) or parsed.path.rsplit('/', 1)[-1]
    return result


def fetch(url, cfg):
    if urlparse(url).scheme != 'https' or urlparse(url).hostname not in cfg['allowed_hosts']:
        raise ValueError('URL fuori dalle fonti GW consentite')
    headers = {'User-Agent': 'TyranidsRosterMonitor/1.0', 'Cache-Control': 'no-cache'}
    data = None
    if urlparse(url).path == '/api/search/downloads/':
        language = parse_qs(urlparse(url).query).get('monitor_language', ['english'])[0]
        data = json.dumps({'index': 'downloads_v2', 'searchTerm': '',
            'gameSystem': 'warhammer-40000', 'language': language}).encode()
        url = url.split('?')[0]
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=data, headers=headers)
    with urlopen(request, timeout=35) as response:
        if urlparse(response.geturl()).hostname not in cfg['allowed_hosts']:
            raise ValueError('Reindirizzamento fuori dalle fonti GW')
        data = response.read(cfg['max_bytes'] + 1)
        if len(data) > cfg['max_bytes']:
            raise ValueError('Documento oltre il limite dimensionale')
        return data


def pdf_text(data):
    from pypdf import PdfReader
    if not data.startswith(b'%PDF-'):
        raise ValueError('Risposta non PDF')
    text = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
    if len(normalized(text)) < 100:
        raise ValueError('Testo PDF non leggibile: verifica manuale necessaria')
    return normalized(text)


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def run(root=ROOT, getter=fetch, reader=pdf_text):
    cfg = load(root / 'monitoraggio/fonti.json', {})
    state_path = root / 'monitoraggio/stato.json'
    feed_path = root / 'novita.json'
    state = load(state_path, {'schema_version': 1, 'pages': {}, 'documents': {}})
    feed = load(feed_path, {'schema_version': 1, 'target_edition': 11, 'items': []})
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    checks, candidates, baseline_urls = [], {}, set(state.get("baseline_pending", []))
    for page in cfg['pages']:
        try:
            html = getter(page, cfg).decode('utf-8', errors='replace')
            links = discover(html, page, cfg)
            if not links:
                raise ValueError('Nessun PDF pertinente trovato: pagina da verificare, non controllo completato')
            if len(links) > cfg['max_documents']:
                raise ValueError('Troppi PDF: affinare la selezione delle fonti')
            first = page not in state['pages']
            if first:
                baseline_urls.update(links)
            candidates.update(links)
            state['pages'][page] = {'last_success': now, 'documents_found': len(links)}
            checks.append({'url': page, 'status': 'ok', 'baseline': first, 'documents_found': len(links)})
        except Exception as exc:
            checks.append({'url': page, 'status': 'error', 'message': str(exc)})
    # Il Munitorum è ora un servizio web: controlla anche la data pubblicata da GW.
    for page in cfg.get('publication_pages', []):
        try:
            parser = Links()
            parser.feed(getter(page, cfg).decode('utf-8', errors='replace'))
            markers = [(url, normalized(label)) for url, label in parser.items
                if urlparse(url).hostname == 'mfm.warhammer-community.com' and normalized(label)]
            if not markers:
                raise ValueError('Indicatore Munitorum non trovato: controllo da verificare')
            marker = json.dumps(sorted(markers))
            key = 'gw_munitorum_publication'
            digest = sha(marker)
            old = state['documents'].get(key)
            event_id = sha(key + digest)
            if old and old['text_sha256'] != digest and event_id not in {x['id'] for x in feed['items']}:
                feed['items'].append({'id': event_id, 'detected_at': now,
                    'source': 'GW ufficiale', 'url': page, 'title': 'Munitorum Field Manual',
                    'status': 'da_verificare', 'kind': 'indicatore_pubblicazione_modificato',
                    'edition': None, 'target_edition_verified': False,
                    'material_change_confirmed': False, 'database_ready': False,
                    'summary': 'GW ha modificato il collegamento o la data del Munitorum. Verificare i punti Tyranids nel documento ufficiale.',
                    'before_sha256': old['text_sha256'], 'after_sha256': digest})
            state['documents'][key] = {'title': 'Munitorum: indicatore pubblicazione',
                'text_sha256': digest, 'edition': None, 'last_success': now}
            checks.append({'url': page, 'status': 'ok', 'scope': 'indicatore Munitorum', 'publication_marker': marker})
        except Exception as exc:
            checks.append({'url': page, 'status': 'error', 'message': str(exc)})
    # Limite globale: mai dichiarare successo dopo un controllo troncato.
    if len(candidates) > cfg['max_documents']:
        checks.append({'status': 'error', 'message': 'Limite globale documenti superato'})
        candidates = {}
    known_hashes = {x['text_sha256'] for x in state['documents'].values()}
    ids = {item['id'] for item in feed['items']}
    for url, title in sorted(candidates.items()):
        try:
            text = reader(getter(url, cfg))
            digest = sha(text)
            previous = state['documents'].get(url)
            edition = edition_evidence(text)
            first_reference = url in baseline_urls or not state.get('baseline_completed', False)
            changed = previous is None or previous['text_sha256'] != digest
            event_id = sha(url + '\n' + digest)
            if changed and not first_reference and digest not in known_hashes and event_id not in ids:
                # Non inventa punti/regole prima-dopo, né presume pertinenza o edizione.
                feed['items'].append({
                    'id': event_id, 'detected_at': now, 'source': 'GW ufficiale',
                    'url': url, 'title': title, 'status': 'da_verificare',
                    'kind': 'documento_modificato' if previous else 'documento_nuovo',
                    'edition': edition, 'target_edition_verified': edition == cfg['target_edition'],
                    'tyranids_mentioned': bool(re.search(r'tyranid|tiranid', text, re.I)),
                    'material_change_confirmed': False, 'database_ready': False,
                    'before_sha256': previous['text_sha256'] if previous else None,
                    'after_sha256': digest,
                    'summary': 'Documento ufficiale nuovo o modificato. Verificare edizione, pertinenza Tyranids e modifiche effettive prima di aggiornare il database.'
                })
                ids.add(event_id)
            baseline_urls.discard(url)
            state['documents'][url] = {'title': title, 'text_sha256': digest,
                'edition': edition, 'last_success': now}
            known_hashes.add(digest)
            checks.append({'url': url, 'status': 'ok', 'edition': edition})
        except Exception as exc:
            # Conserva il riferimento precedente, per riprendere dopo un errore.
            checks.append({'url': url, 'status': 'error', 'message': str(exc)})
    if state['documents']:
        state['baseline_completed'] = True
    errors = sum(c['status'] == 'error' for c in checks)
    state['baseline_pending'] = sorted(baseline_urls)
    state['last_check'] = now
    state['checks'] = checks
    feed['last_check'] = now
    feed['health'] = 'partial' if errors else 'ok'
    feed['checks'] = checks
    save(state_path, state)
    save(feed_path, feed)
    summary = f'Controllo GW: {len(candidates)} documenti individuati; {errors} errori; {len(feed["items"])} avvisi conservati.\n'
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
            out.write(summary + '\nGli avvisi sono da verificare. Manifest e database non vengono modificati.\n')
    return 0  # Pubblica anche gli errori nel feed, senza perdere il riferimento.


if __name__ == '__main__':
    sys.exit(run())

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from difflib import SequenceMatcher
import json, os, urllib.request, urllib.error, datetime, re, unicodedata

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)


def load_env():
    p = ROOT / '.env'
    if not p.exists():
        return
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
USAGE_FILE = ROOT / 'search_usage.json'
JST = datetime.timezone(datetime.timedelta(hours=9))


def clean_page_text(text):
    """検索エンジンが返した本文から、広告・ナビ・フッター等のノイズを減らす。"""
    if not text:
        return ''
    text = text.replace('\r', '\n')
    noise = [
        r'広告', r'スポンサー', r'cookie', r'クッキー', r'プライバシーポリシー',
        r'サイトマップ', r'お問い合わせ', r'ログイン', r'会員登録',
        r'公式オンラインショップ', r'関連サイト', r'ページトップ',
        r'メニュー', r'検索する', r'\bmenu\b', r'\bfooter\b', r'\bheader\b',
        r'\bsidebar\b', r'\badvertisement\b', r'\bsponsored\b'
    ]
    cleaned, seen = [], set()
    for raw in text.splitlines():
        line = re.sub(r'\s+', ' ', raw).strip(' ・*\t')
        if len(line) < 8 or any(re.search(pat, line, re.I) for pat in noise):
            continue
        if re.fullmatch(r'https?://\S+', line):
            continue
        if len(re.sub(r'[\W_]', '', line)) < 5:
            continue
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(line)
    return '\n'.join(cleaned)[:700]


def usage():
    today = datetime.datetime.now(JST).date().isoformat()
    try:
        data = json.loads(USAGE_FILE.read_text(encoding='utf-8'))
    except Exception:
        data = {}
    if data.get('date') != today:
        data = {'date': today, 'count': 0}
    return data


def normalized_title(title):
    """媒体名や記号の違いをならし、見出し同士を比較しやすくする。"""
    value = unicodedata.normalize('NFKC', title or '').casefold()
    value = re.sub(r'https?://\S+', '', value)
    value = re.sub(r'[【】\[\]（）()「」『』〈〉《》：:｜|・･!！?？.,，。／/\\\-‐‑–—_　\s]', '', value)
    # 媒体名・告知ラベルだけの差を吸収
    for word in ('公式サイト', '公式twitter', '公式x', 'お知らせ', 'ニュース', 'news', 'event', 'イベント情報'):
        value = value.replace(word, '')
    return value


def title_date_tokens(title):
    """タイトルに明記された日付。異なる日付が明記された記事は統合しない。"""
    text = unicodedata.normalize('NFKC', title or '')
    found = set()
    for m in re.finditer(r'(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})', text):
        found.add(f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}')
    return found


def is_same_announcement(a, b):
    # 作品名が違う検索結果は統合しない
    if a.get('work') != b.get('work'):
        return False
    ta, tb = normalized_title(a.get('title')), normalized_title(b.get('title'))
    if not ta or not tb:
        return False
    da, db = title_date_tokens(a.get('title')), title_date_tokens(b.get('title'))
    if da and db and da.isdisjoint(db):
        return False
    if ta == tb:
        return True
    # 類似タイトルはかなり近い場合だけ統合する（誤統合を抑える）
    return SequenceMatcher(None, ta, tb).ratio() >= 0.94 and min(len(ta), len(tb)) >= 8

def official_score(item):
    domain = urlparse(item.get('url', '')).netloc.lower()
    title = item.get('title', '').lower()

    score = 0

    if domain in ('x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'):
        score += 2

    if any(word in title for word in ('公式', 'official')):
        score += 2

    return score
def merge_duplicate_results(items):
    """同一告知らしい検索結果を1件にまとめ、情報源URLを保持する。"""
    groups = []
    for item in items:
        match = next((g for g in groups if is_same_announcement(g[0], item)), None)
        if match is None:
            copy = dict(item)
            copy['sources'] = [{'title': item['title'], 'url': item['url'], 'domain': item['domain']}]
            groups.append([copy])
        else:
            if not any(s['url'] == item['url'] for s in match[0]['sources']):
                match[0]['sources'].append({'title': item['title'], 'url': item['url'], 'domain': item['domain']})
            # 本文が長い方を代表本文として採用
            if len(item.get('content', '')) > len(match[0].get('content', '')):
                match[0]['content'] = item['content']
            # 公開日が確認できるものを代表日付にする
            if item.get('published_date') and (not match[0].get('published_date') or item['published_date'] > match[0]['published_date']):
                match[0]['published_date'] = item['published_date']
                match[0]['date_label'] = item['date_label']
    merged = []
    for group in groups:
        item = group[0]
        sources = item['sources']
        if len(sources) > 1:
            source_lines = ['\n\n関連する情報源：']
            source_lines.extend(f"・{s['domain']}：{s['url']}" for s in sources)
            item['content'] = (item.get('content', '') + ''.join(source_lines))[:1400]
        merged.append(item)
    return merged


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if urlparse(self.path).path != '/api/search':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            works = payload.get('works', [])
            works = [str(w).strip() for w in works if str(w).strip()][:20]
            if not works:
                return self.reply(400, {'error': '検索する作品がありません。'})
            key = os.environ.get('TAVILY_API_KEY', '').strip()
            if not key:
                return self.reply(400, {'error': 'APIキーが未設定です。.envファイルを確認してください。'})
            state = usage()
            if state['count'] >= 3:
                return self.reply(429, {'error': '本日の検索上限（3回）に達しました。日本時間0時にリセットされます。'})
            year = datetime.datetime.now(JST).year
            query = ' OR '.join(f'"{w}"' for w in works) + f' 公式サイト 公式X 公式発表 最新 新着 {year} イベント コラボカフェ 展示会 キャンペーン グッズ 発売 予約 開催 site:x.com OR site:twitter.com'
            body = json.dumps({'query': query, 'topic': 'general', 'search_depth': 'advanced', 'max_results': 20, 'include_answer': False, 'time_range': 'month'}).encode('utf-8')
            req = urllib.request.Request('https://api.tavily.com/search', data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=45) as res:
                    result = json.loads(res.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', 'replace')[:800]
                return self.reply(502, {'error': f'Tavily HTTP {e.code}: {detail}'})
            state['count'] += 1
            USAGE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            out = []
            for item in result.get('results', []):
                url = item.get('url', '')
                if not url.startswith(('http://', 'https://')):continue
                page_title = (item.get('title') or url).strip()
                content = clean_page_text((item.get('content') or '').strip())
                search_text = (page_title + ' ' + content).lower()
                if not any(work.lower() in search_text for work in works):continue
                # アフィリエイト・商品紹介中心の記事を除外
                if any(word in page_title + ' ' + content for word in (
                   'アフィリエイト', 'アフィリエイト広告',
                   'おすすめランキング', 'ランキング形式で紹介',
                   '買ってよかった', '実際に買ってみた',
                   'PR案件', '広告記事'
               )):
                   continue
                bullets = re.findall(r'(?:^|\n)\s*(?:[＊*・]|[-•])\s*[^\n]{8,}', content)
                if len(bullets) >= 2 or ('NEWS' in page_title.upper() and len(re.findall(r'20\d{2}年', content)) >= 2):
                    continue
                published = item.get('published_date') or item.get('publishedDate') or item.get('date')
                date_label, sort_date = '日付不明', ''
                if published:
                    try:
                        parsed = datetime.datetime.fromisoformat(str(published).replace('Z', '+00:00'))
                        sort_date = parsed.date().isoformat()
                        date_label = sort_date
                    except Exception:
                        m = re.search(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})', str(published))
                        if m:
                            sort_date = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
                            date_label = sort_date
                out.append({'title': page_title, 'url': url, 'content': content, 'domain': urlparse(url).netloc, 'work': '・'.join(works), 'published_date': sort_date, 'date_label': date_label})
            out = merge_duplicate_results(out)
            out.sort(key=lambda x: (official_score(x), bool(x['published_date']), x['published_date']), reverse=True)
            self.reply(200, {'results': out, 'remaining': max(0, 3 - state['count'])})
        except Exception as e:
            self.reply(500, {'error': str(e)})

    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(fmt % args)


print('Lumiere を起動しました: http://127.0.0.1:8000')
print('APIキー認証モード（.env を読み込みます）')
print('終了するときは、この黒い画面で Ctrl + C を押してください。')
port = int(os.environ.get("PORT", 8000))
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

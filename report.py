"""지씨셀 IR 데일리 브리핑 (키움증권 REST API 버전)

- 키움 REST API: 주가/거래량(ka10001), 투자자별 수급(ka10059), 코스피·코스닥·코스닥 제약(KQ26) 지수(ka20001, ka10101)
- 키움에 없는 항목: 나스닥 바이오(NBI)·원/달러 = Yahoo Finance
- 뉴스: 지씨셀 = 네이버 검색 API(키 필요, 없으면 구글로 대체)
        NBI = 상위 구성종목(기본 8개)별 최신 뉴스(Google News RSS, 24시간 내 -> 부족하면 3일)
- 결과: output/dashboard_YYYYMMDD.html (데이터가 내장된 단일 HTML) + 메일 발송

사용법
  python report.py            # 일일 보고서 생성 + 메일 발송
  python report.py --probe    # 키움 원본 응답 출력 (필드/단위 확인용, 메일 발송 없음)
  python serve.py             # 로컬 대시보드 서버: 새로고침 버튼으로 시세·수급·뉴스를 새로 수집

설정(.env 또는 환경변수)
  KIWOOM_APPKEY, KIWOOM_SECRETKEY   키움 REST API 앱키/시크릿키 (필수)
  KIWOOM_MOCK=1                     모의투자 서버 사용 (기본 0)
  MAIL_USER, MAIL_PASS, MAIL_TO     메일 발송 (없으면 HTML만 저장)
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  네이버 검색 API 키 (지씨셀 뉴스용)
  NBI_STOCKS                        NBI 뉴스 대상 종목 변경 ("한글|영문,..." 형식, 선택)
  PHARMA_CODE                       코스닥 제약(KQ26) 키움 업종코드 (기본 120, 바꿀 때만 지정)
  FORCE=1                           휴장일에도 강제 실행

주의: 키움 REST API는 토큰 발급 PC의 IP를 사전에 등록해야 합니다(최대 10개).
"""
import calendar
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import requests

HERE = Path(__file__).parent
OUT_DIR = HERE / "output"
CODE, NAME = "144510", "지씨셀"
KST = timezone(timedelta(hours=9))

# ka10059(금액 기준) 응답 단위. 문서/예제 기준 백만원으로 가정 -> --probe 로 실제 값과 대조해 확인할 것
INVESTOR_UNIT_WON = 1_000_000

INTERNAL_TEXT = (
    "지씨셀 및 관계사(아티바 바이오테라퓨틱스 등) 관련 주요 뉴스 피드를 모니터링하고, "
    "NK세포치료제 등 핵심 파이프라인의 임상 진전 및 R&D 가시성에 따른 주가 변동성 반영."
)


# ---------- 공통 유틸 ----------
def load_env():
    """같은 폴더의 .env 를 읽어 환경변수로 등록 (이미 설정된 값은 유지)."""
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def safe(fn, default, label):
    """한 항목이 실패해도 전체 보고서는 계속 만든다."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"[경고] {label} 실패: {e}", file=sys.stderr)
        return default


def num(s):
    """키움 응답의 '+61300', '-2394.49', '--28837', '1,234' 같은 문자열을 float 로."""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if not t:
        return None
    neg = t.startswith("-")
    t = t.lstrip("+-")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def pct_str(v):
    return f"{v:+.2f}%"


def eok(won):
    return f"{won / 1e8:+,.1f}억"


def tone(s):
    """한국식: 상승 빨강 / 하락 파랑 / 0·없음 회색"""
    v = num(str(s or "").replace("억", "").replace("%", ""))
    if v is None or v == 0:
        return "#666"
    return "#d62728" if v > 0 else "#1f5fbf"


# ---------- 키움 REST API 클라이언트 ----------
class Kiwoom:
    def __init__(self, appkey, secretkey, mock=False):
        self.appkey, self.secretkey = appkey, secretkey
        self.host = "https://mockapi.kiwoom.com" if mock else "https://api.kiwoom.com"
        self.token = None

    def login(self):
        r = requests.post(
            f"{self.host}/oauth2/token",
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json={"grant_type": "client_credentials", "appkey": self.appkey, "secretkey": self.secretkey},
            timeout=10,
        )
        d = r.json()
        if not d.get("token") or d.get("return_code") not in (None, 0, "0"):
            raise RuntimeError(
                f"토큰 발급 실패: {d.get('return_msg')} "
                "(키움 REST API 포털 > 계좌 APP Key 관리에서 현재 PC의 IP를 등록했는지 확인)"
            )
        self.token = d["token"]

    def post(self, path, api_id, body, retries=3):
        for i in range(retries):
            r = requests.post(
                self.host + path,
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "api-id": api_id,
                    "authorization": f"Bearer {self.token}",
                },
                json=body,
                timeout=15,
            )
            if r.status_code == 429:  # 요청 제한: 잠시 쉬고 재시도
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            d = r.json()
            # 업무 오류도 HTTP 200 으로 오므로 return_code 로 판별
            if d.get("return_code") not in (0, "0"):
                raise RuntimeError(f"{api_id} 오류: {d.get('return_msg')}")
            time.sleep(0.3)
            return d
        raise RuntimeError(f"{api_id}: 요청 제한(429)이 계속됨")

    def revoke(self):
        if not self.token:
            return
        try:
            requests.post(
                f"{self.host}/oauth2/revoke",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={"appkey": self.appkey, "secretkey": self.secretkey, "token": self.token},
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass


# ---------- 키움 데이터 ----------
def stock_block(kw):
    d = kw.post("/api/dostk/stkinfo", "ka10001", {"stk_cd": CODE})
    price, ratio, vol = num(d.get("cur_prc")), num(d.get("flu_rt")), num(d.get("trde_qty"))
    if price is None or ratio is None:
        raise RuntimeError(f"ka10001 응답 필드 확인 필요: {list(d)[:12]}")
    return {
        "price": f"{abs(price):,.0f}원",
        "ratio": pct_str(ratio),
        "volume": f"{abs(vol or 0):,.0f}주",
        "marketStatus": "정규장 마감",
    }


def investor_rows(kw, ymd):
    d = kw.post(
        "/api/dostk/stkinfo",
        "ka10059",
        {"dt": ymd, "stk_cd": CODE, "amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000"},
    )
    rows = d.get("stk_invsr_orgn") or []
    if not rows:
        raise RuntimeError(f"ka10059 응답에 일별 데이터가 없음: {list(d)[:8]}")
    return sorted(rows, key=lambda r: r.get("dt", ""), reverse=True)


def investor_block(rows):
    cols = {"foreign": "frgnr_invsr", "organ": "orgn", "personal": "ind_invsr"}

    def total(sub, col):
        return sum((num(r.get(col)) or 0) for r in sub) * INVESTOR_UNIT_WON

    return {
        **{k: eok(total(rows[:1], c)) for k, c in cols.items()},
        "5d": {k: eok(total(rows[:5], c)) for k, c in cols.items()},
    }


def sector_index(kw, mrkt_tp, inds_cd):
    d = kw.post("/api/dostk/sect", "ka20001", {"mrkt_tp": mrkt_tp, "inds_cd": inds_cd})
    price, ratio = num(d.get("cur_prc")), num(d.get("flu_rt"))
    if price is None or ratio is None:
        raise RuntimeError(f"ka20001 응답 필드 확인 필요: {list(d)[:12]}")
    return {"value": f"{abs(price):,.2f}", "ratio": pct_str(ratio)}


def sector_names(kw, mrkt_tp):
    """ka10101(업종코드 리스트) -> {업종코드: 업종명}"""
    d = kw.post("/api/dostk/stkinfo", "ka10101", {"mrkt_tp": mrkt_tp})
    out = {}
    for v in d.values():
        if not isinstance(v, list):
            continue
        for it in v:
            if isinstance(it, dict):
                code = it.get("code") or it.get("inds_cd")
                name = str(it.get("name") or it.get("inds_nm") or "").strip()
                if code and name:
                    out[str(code)] = name
    return out


PHARMA_CODE_DEFAULT = "120"  # 키움 지수차트의 '제약 120' = 코스닥 제약(KQ26)


def kosdaq_pharma(kw):
    """KQ26(코스닥 제약). KQ26 은 Investing.com 표기이고 키움에서는 업종코드 120 이다."""
    code = os.environ.get("PHARMA_CODE") or PHARMA_CODE_DEFAULT
    name = safe(lambda: sector_names(kw, "1"), {}, "업종코드 목록(ka10101)").get(code)
    if name and "제약" not in name:
        print(f"[경고] 업종코드 {code} 의 업종명은 '{name}' 입니다. 코스닥 제약이 맞는지 확인하세요.", file=sys.stderr)
    else:
        print(f"코스닥 제약 업종코드: {code}" + (f" ({name})" if name else ""))
    return sector_index(kw, "1", code)


# ---------- 키움 외 데이터 ----------
def yf_pair(symbol):
    import yfinance as yf

    h = yf.Ticker(symbol).history(period="10d")["Close"].dropna()
    return float(h.iloc[-1]), float(h.iloc[-2])


def nbi_index():
    last, prev = yf_pair("^NBI")
    return {"value": f"{last:,.2f}", "ratio": pct_str((last - prev) / prev * 100)}


def usd_krw():
    last, prev = yf_pair("KRW=X")
    return {"value": f"{last:,.2f}", "change": pct_str((last - prev) / prev * 100)}


def _entries(query):
    """Google News RSS -> [(KST datetime|None, item dict)]"""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    out = []
    for e in feedparser.parse(url).entries:
        link = e.get("link", "")
        if urlparse(link).scheme not in ("http", "https"):
            continue
        publisher = (e.get("source") or {}).get("title", "뉴스")
        title = e.get("title", "")
        if title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        p = e.get("published_parsed")
        dt = datetime.fromtimestamp(calendar.timegm(p), KST) if p else None
        out.append(
            (
                dt,
                {
                    "date": f"{dt.month:02d}/{dt.day:02d}" if dt else "최신",
                    "title": title,
                    "publisher": publisher,
                    "linkUrl": link,
                },
            )
        )
    return out


def _newest(pairs):
    return sorted(pairs, key=lambda x: x[0] or datetime.min.replace(tzinfo=KST), reverse=True)


def news(query, limit=3):
    return [it for _, it in _entries(query)[:limit]]


# NBI 상위 구성종목 (비중 큰 순). "한글|영문", 쉼표 구분. .env 의 NBI_STOCKS 로 바꿀 수 있음
NBI_STOCKS_DEFAULT = (
    "암젠|Amgen,길리어드|Gilead,버텍스|Vertex Pharmaceuticals,리제네론|Regeneron,"
    "알닐람|Alnylam,인스메드|Insmed,아르젠엑스|argenx,레볼루션 메디슨|Revolution Medicines"
)


def nbi_news(limit=3):
    """NBI 상위 종목별 최신 뉴스를 모아, 종목이 겹치지 않게 최신순 limit 건. 24시간 내 기사가 부족하면 3일로 넓힌다."""
    raw = os.environ.get("NBI_STOCKS") or NBI_STOCKS_DEFAULT
    stocks = [x.strip().split("|", 1) for x in raw.split(",") if x.strip()]
    for days in (1, 3):
        firsts, seconds, seen = [], [], set()
        for st in stocks:
            ko = st[0].strip()
            q = f'"{ko}"' + (f' OR "{st[1].strip()}"' if len(st) > 1 else "")
            pairs = safe(lambda: _entries(f"({q}) when:{days}d"), [], f"NBI 뉴스({ko})")
            time.sleep(0.2)
            picked = []
            for dt, it in _newest(pairs):
                key = it["linkUrl"] or it["title"]
                if key in seen:
                    continue
                seen.add(key)
                picked.append((dt, {**it, "title": f"[{ko}] {it['title']}"}))
                if len(picked) == 2:
                    break
            if picked:
                firsts.append(picked[0])
            if len(picked) > 1:
                seconds.append(picked[1])
        chosen = _newest(firsts)[:limit]
        if len(chosen) < limit:
            chosen += _newest(seconds)[: limit - len(chosen)]
        if chosen:
            return [it for _, it in _newest(chosen)]
    return []


def naver_news(queries, limit=3):
    """네이버 검색 API(뉴스)에서 최신순으로 가져온다. 키 없으면 예외 -> 호출부에서 구글로 대체."""
    cid, csec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
    items, seen = [], set()
    for q in queries:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
            params={"query": q, "display": 10, "sort": "date"},
            timeout=10,
        )
        r.raise_for_status()
        for it in r.json().get("items", []):
            title = unescape(re.sub(r"<[^>]+>", "", it.get("title", ""))).strip()
            link = it.get("link") or it.get("originallink") or ""
            if not title or title in seen or urlparse(link).scheme not in ("http", "https"):
                continue
            seen.add(title)
            try:
                dt = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except Exception:  # noqa: BLE001
                dt = None
            host = urlparse(it.get("originallink") or link).netloc.removeprefix("www.")
            items.append((dt or datetime.min.replace(tzinfo=KST), {
                "date": f"{dt.month:02d}/{dt.day:02d}" if dt else "최신",
                "title": title,
                "publisher": host or "네이버뉴스",
                "linkUrl": link,
            }))
    items.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in items[:limit]]


def company_news():
    try:
        return naver_news([NAME, "아티바"])
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 네이버 뉴스 실패 -> 구글 뉴스로 대체: {e}", file=sys.stderr)
        return news(f"{NAME} OR 아티바 when:2d")


# ---------- 분석 문구 / HTML / 메일 ----------
def analysis_block(inv, macro, company_news_list):
    """동적 IR 톤 주가변동 요약 분석 문구 생성 (3가지 핵심 축 기준)"""
    news_titles = [n.get("title", "") for n in (company_news_list or [])]
    if any("아티바" in t or "개발" in t or "임상" in t for t in news_titles):
        internal_analysis = (
            "관계사(아티바 등) 파이프라인 이슈 및 파급 영향으로 인한 주가 변동성 확대. "
            "핵심 파이프라인 임상 가시성 재증명 및 내부 지배구조/책임경영 시그널 모니터링 필요."
        )
    else:
        internal_analysis = (
            "지씨셀 및 파트너사 관련 주요 키워드 피드 모니터링. "
            "NK세포치료제 파이프라인의 임상 진전 및 R&D 가시성 확보 모멘텀 지속 반영 중."
        )

    def g(k, f="ratio"):
        return (macro.get(k) or {}).get(f, "-")

    nbi_r = g('nbi')
    kq26_r = g('kq26')
    
    industry_analysis = (
        f"글로벌 바이오(NBI {nbi_r}) 지수와 domestic 코스닥 제약(KQ26 {kq26_r}) 지수의 흐름 속 차별화. "
        f"투자자별 수급(금일 외국인 {inv['foreign'] if inv else '-'}, 기관 {inv['organ'] if inv else '-'})에 따른 세부 종목별 디커플링 심화."
    )

    macro_analysis = (
        f"국내 증시 전체 변동성(코스피 {g('kospi')}, 코스닥 {g('kosdaq')}) 및 원/달러 환율({g('usdKrw', 'change')})의 수급 영향성 반영. "
        "외국인 및 기관 수급의 민감도 상승에 따른 중소형 바이오 섹터 영향 반영."
    )

    return {
        "internal": internal_analysis,
        "supply": industry_analysis,
        "macro": macro_analysis,
    }


def render_dashboard(data):
    tpl = (HERE / "index.html").read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in tpl:
        raise RuntimeError("index.html 에 데이터 삽입 자리(/*__DATA__*/null)가 없음")
    payload = (
        json.dumps(data, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return tpl.replace(marker, payload)


def build_email(d):
    s, inv, m = d["stock"], d["stock"].get("investor"), d["macroIndices"]

    def row(label, val, chg):
        return (
            f"<tr><td>{escape(label)}</td><td align='right'>{escape(str(val or '-'))}</td>"
            f"<td align='right' style='color:{tone(chg)}'>{escape(str(chg or '-'))}</td></tr>"
        )

    def news_ul(items):
        if not items:
            return "<p style='color:#888'>최근 뉴스가 없습니다.</p>"
        li = "".join(
            f"<li style='margin-bottom:6px'><a href='{escape(n['linkUrl'])}'>{escape(n['title'])}</a> "
            f"<span style='color:#888'>({escape(n['publisher'])})</span></li>"
            for n in items
        )
        return f"<ul>{li}</ul>"

    if inv:
        inv_rows = "".join(
            f"<tr><td>{lb}</td><td align='right' style='color:{tone(inv[k])}'>{inv[k]}</td>"
            f"<td align='right' style='color:{tone(inv['5d'][k])}'>{inv['5d'][k]}</td></tr>"
            for lb, k in (("외국인", "foreign"), ("기관", "organ"), ("개인", "personal"))
        )
        inv_html = (
            "<table cellpadding='6' width='100%' style='border-collapse:collapse'>"
            "<tr style='background:#f3f3f3'><th align='left'>투자자별</th>"
            "<th align='right'>금일 마감</th><th align='right'>최근 5일</th></tr>"
            f"{inv_rows}</table>"
        )
    else:
        inv_html = "<p style='color:#888'>수급 데이터를 가져오지 못했습니다.</p>"

    a = d["analysis"]
    tbl = "<table cellpadding='6' width='100%' style='border-collapse:collapse'>"
    return f"""
<div style="font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:600px;color:#222">
  <h2>{NAME} IR 데일리 브리핑 - {d['tradingDate']}</h2>
  <h3>1. {NAME} 수급 및 주가</h3>
  <p style="font-size:26px;margin:0"><b>{escape(s['price'])}</b>
     <span style="color:{tone(s['ratio'])};font-size:17px">{escape(s['ratio'])}</span></p>
  <p style="color:#666;margin-top:4px">거래량 {escape(s['volume'])} · {escape(s['marketStatus'])}</p>
  {inv_html}
  <h3>2. 주요 이슈 ({NAME} · 아티바)</h3>
  {news_ul(d['companyNews'])}
  <h3>3. 오늘의 포인트 &amp; IR 주가변동 3대 요인 분석</h3>
  <ul>
    <li><b>1. 기사 기반 기업 측면</b>: {escape(a['internal'])}</li>
    <li><b>2. 업계 기반 (코스닥제약/NBI/수급)</b>: {escape(a['supply'])}</li>
    <li><b>3. 매크로 기반 (증시/환율)</b>: {escape(a['macro'])}</li>
  </ul>
  <h3>4. 업종 지수 및 매크로 지표</h3>
  {tbl}
    {row('나스닥 바이오(NBI)', m.get('nbi', {}).get('value'), m.get('nbi', {}).get('ratio'))}
    {row('코스닥 제약 (KQ26)', m.get('kq26', {}).get('value'), m.get('kq26', {}).get('ratio'))}
    {row('코스피', m.get('kospi', {}).get('value'), m.get('kospi', {}).get('ratio'))}
    {row('코스닥', m.get('kosdaq', {}).get('value'), m.get('kosdaq', {}).get('ratio'))}
    {row('원/달러', m.get('usdKrw', {}).get('value'), m.get('usdKrw', {}).get('change'))}
  </table>
  <h3>5. NBI 관련 주요 뉴스</h3>
  {news_ul(d['nbiNews'])}
  <p style="color:#999;font-size:12px">{d['updatedAt']} 기준 자동 생성 · 첨부한 HTML 파일을 열면 대시보드 화면으로 볼 수 있습니다.</p>
</div>"""


def send_mail(subject, body_html, attach_name, attach_html):
    user, pw, to = os.environ["MAIL_USER"], os.environ["MAIL_PASS"], os.environ["MAIL_TO"]
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    att = MIMEText(attach_html, "html", "utf-8")
    att.add_header("Content-Disposition", "attachment", filename=attach_name)
    msg.attach(att)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


def probe(kw, ymd):
    calls = [
        ("ka10001 주식기본정보", "/api/dostk/stkinfo", "ka10001", {"stk_cd": CODE}),
        (
            "ka10059 종목별투자자기관별",
            "/api/dostk/stkinfo",
            "ka10059",
            {"dt": ymd, "stk_cd": CODE, "amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000"},
        ),
        ("ka10101 업종코드(코스닥)", "/api/dostk/stkinfo", "ka10101", {"mrkt_tp": "1"}),
        ("ka20001 업종현재가(코스피)", "/api/dostk/sect", "ka20001", {"mrkt_tp": "0", "inds_cd": "001"}),
    ]
    for title, path, api_id, body in calls:
        print(f"\n===== {title} =====")
        try:
            print(json.dumps(kw.post(path, api_id, body), ensure_ascii=False, indent=1)[:1800])
        except Exception as e:  # noqa: BLE001
            print("실패:", e)


def collect(kw, require_today=True):
    now = datetime.now(KST)
    ymd = now.strftime("%Y%m%d")

    rows = safe(lambda: investor_rows(kw, ymd), None, "수급(ka10059)")
    last_dt = rows[0].get("dt") if rows else None
    if require_today and last_dt and last_dt != ymd and not os.environ.get("FORCE"):
        print(f"오늘({ymd})은 거래일이 아니거나 데이터 미반영. 마지막 거래일: {last_dt}")
        return None

    st = stock_block(kw)
    if last_dt and last_dt != ymd:
        st["marketStatus"] = "전 거래일 마감 기준"
    elif now.hour * 60 + now.minute >= 15 * 60 + 30:
        st["marketStatus"] = "정규장 마감"
    else:
        st["marketStatus"] = "장중 (실시간)"

    inv = safe(lambda: investor_block(rows), None, "수급 집계") if rows else None
    if inv:
        st["investor"] = inv

    macro = {
        "nbi": safe(nbi_index, None, "NBI"),
        "kq26": safe(lambda: kosdaq_pharma(kw), None, "코스닥 제약"),
        "kospi": safe(lambda: sector_index(kw, "0", "001"), None, "코스피"),
        "kosdaq": safe(lambda: sector_index(kw, "1", "101"), None, "코스닥"),
        "usdKrw": safe(usd_krw, None, "원/달러"),
    }
    macro = {k: v for k, v in macro.items() if v}

    company_news_list = safe(company_news, [], "당사 뉴스")

    day = last_dt or ymd
    data = {
        "updatedAt": f"{now:%Y-%m-%d %H:%M} KST",
        "tradingDate": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "stock": st,
        "companyNews": company_news_list,
        "nbiNews": safe(nbi_news, [], "NBI 종목 뉴스"),
        "macroIndices": macro,
    }
    data["analysis"] = analysis_block(inv, macro, company_news_list)
    return data


def main():
    load_env()
    appkey, secret = os.environ.get("KIWOOM_APPKEY"), os.environ.get("KIWOOM_SECRETKEY")
    if not appkey or not secret:
        sys.exit("KIWOOM_APPKEY / KIWOOM_SECRETKEY 가 설정되지 않았습니다 (.env 확인)")

    kw = Kiwoom(appkey, secret, mock=os.environ.get("KIWOOM_MOCK") == "1")
    kw.login()
    try:
        if "--probe" in sys.argv:
            probe(kw, datetime.now(KST).strftime("%Y%m%d"))
            return

        data = collect(kw)
        if not data:
            return
        st, day = data["stock"], data["tradingDate"].replace("-", "")

        OUT_DIR.mkdir(exist_ok=True)
        page = render_dashboard(data)
        (OUT_DIR / f"dashboard_{day}.html").write_text(page, encoding="utf-8")
        (OUT_DIR / "latest.html").write_text(page, encoding="utf-8")
        (OUT_DIR / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("저장 완료:", OUT_DIR / f"dashboard_{day}.html")

        if os.environ.get("MAIL_USER") and os.environ.get("MAIL_PASS") and os.environ.get("MAIL_TO"):
            subject = f"[{NAME} IR 데일리 브리핑] {data['tradingDate'][5:].replace('-', '/')} 종가 {st['price']} ({st['ratio']})"
            send_mail(subject, build_email(data), f"gccell_{day}.html", page)
            print("메일 발송 완료:", subject)
        else:
            print("메일 설정 없음: HTML만 저장했습니다.")
    finally:
        kw.revoke()


if __name__ == "__main__":
    main()

import os, re, ipaddress, socket
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
import requests
from bs4 import BeautifulSoup
import trafilatura
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

# Only official/public institutional domains are eligible for retrieval.
ALLOWED_DOMAINS = (
    "gov.in", "nic.in", "india.gov.in", "indiacode.nic.in",
    "myscheme.gov.in", "services.india.gov.in", "nalsa.gov.in",
    "uidai.gov.in", "rural.gov.in", "wcd.gov.in", "labour.gov.in",
    "pib.gov.in", "sansad.in", "eci.gov.in", "jharkhand.gov.in",
    "jharkhand.nic.in", "pmkisan.gov.in", "pmjay.gov.in"
)
HEADERS = {"User-Agent": "CivicSaathiAI/1.0 (civic information prototype)"}
TIMEOUT = 15
app = FastAPI(title="CivicSaathi AI API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=[ORIGIN, "http://127.0.0.1:5173"],
    allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"]
)

class SearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1500)
    language: str = "English"

class TranslateUIRequest(BaseModel):
    language: str = Field(min_length=2, max_length=40)
    strings: dict

class TranslateTextRequest(BaseModel):
    language: str = Field(min_length=2, max_length=40)
    text: str = Field(min_length=1, max_length=8000)

class IngestRequest(BaseModel):
    url: str

class Source(BaseModel):
    url: str
    title: str = ""
    text: str = ""

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1500)
    language: str = "English"
    sources: List[Source] = Field(min_length=1, max_length=20)

def is_allowed_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)

def validate_official_url(url: str):
    try:
        p = urlparse(url)
        if p.scheme != "https" or not p.hostname or not is_allowed_host(p.hostname):
            raise ValueError("Only HTTPS pages on approved official government/public-institution domains are allowed.")
        # Reject IP literals and private/reserved destinations.
        try:
            ip = ipaddress.ip_address(p.hostname)
            if not ip.is_global:
                raise ValueError("Non-public network destinations are not allowed.")
        except ValueError as e:
            if "not allowed" in str(e):
                raise
        # Resolve host and reject private/reserved IPs as a basic SSRF safeguard.
        for item in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
            addr = ipaddress.ip_address(item[4][0])
            if not addr.is_global:
                raise ValueError("The URL resolves to a non-public network destination.")
        return p
    except (socket.gaierror, OSError):
        raise ValueError("Could not resolve the official website host.")
    except ValueError:
        raise

def fetch_page(url: str):
    validate_official_url(url)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        if urlparse(r.url).scheme != "https" or not is_allowed_host(urlparse(r.url).hostname):
            raise ValueError("The page redirected outside approved official domains.")
        content_type = r.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise ValueError("This URL does not return an HTML or text page.")
        raw = r.text[:2_000_000]
        extracted = trafilatura.extract(raw, include_comments=False, include_tables=True)
        if not extracted:
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            extracted = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", extracted or "").strip()
        if len(text) < 120:
            raise ValueError("Not enough readable text was found on this page.")
        soup = BeautifulSoup(raw, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else urlparse(url).netloc
        return {"url": r.url, "title": title[:240], "text": text[:24000]}
    except requests.RequestException as e:
        raise ValueError(f"Could not retrieve this page: {str(e)[:180]}")

def search_official(query: str):
    """Search official sources with a few query shapes and DDG HTML/Lite fallbacks."""
    from urllib.parse import parse_qs, urljoin

    clean_query = re.sub(r"\s+", " ", query).strip()
    query_variants = [
        f'{clean_query} site:myscheme.gov.in',
        f'{clean_query} site:india.gov.in',
        f'{clean_query} site:gov.in OR site:nic.in OR site:pib.gov.in',
        f'{clean_query} government scheme eligibility benefits official',
    ]
    results, seen, request_errors = [], set(), []

    def add_results(soup, lite=False):
        nodes = soup.select(".result, article[data-testid='result']")
        if lite:
            nodes += soup.select("tr")
        for node in nodes:
            a = node.select_one(".result__a, a[data-testid='result-title-a'], a.result-link")
            if not a or not a.get("href"):
                continue
            href = a.get("href", "")
            parsed = urlparse(href)
            if "uddg" in parse_qs(parsed.query):
                href = parse_qs(parsed.query)["uddg"][0]
            elif href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin("https://html.duckduckgo.com", href)
            try:
                validate_official_url(href)
            except Exception:
                continue
            if href in seen:
                continue
            seen.add(href)
            snippet_node = node.select_one(".result__snippet, [data-result='snippet'], .result-snippet")
            results.append({
                "url": href,
                "title": a.get_text(" ", strip=True),
                "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
            })
            if len(results) >= 20:
                return

    # Try more than one official-domain query. Some search pages return no results
    # for a long OR expression even when a simpler query finds relevant pages.
    for q in query_variants:
        try:
            r = requests.get("https://html.duckduckgo.com/html/", params={"q": q},
                             headers=HEADERS, timeout=10)
            r.raise_for_status()
            add_results(BeautifulSoup(r.text, "html.parser"))
        except requests.RequestException as e:
            request_errors.append(str(e))
        if len(results) >= 8:
            break

    # DDG's Lite layout is a fallback if its standard HTML layout changes.
    if not results:
        try:
            r = requests.get("https://lite.duckduckgo.com/lite/",
                             params={"q": query_variants[0]}, headers=HEADERS, timeout=10)
            r.raise_for_status()
            add_results(BeautifulSoup(r.text, "html.parser"), lite=True)
        except requests.RequestException as e:
            request_errors.append(str(e))

    if not results and request_errors and len(request_errors) >= len(query_variants):
        raise HTTPException(status_code=502,
            detail="Live search is temporarily unavailable. Please retry, or use the official myScheme portal.")
    return results

def chunks(text, size=1700, overlap=220):
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        out.append(text[start:end])
        if end == len(text): break
        start = end - overlap
    return out

def rank_evidence(question, sources):
    terms = {w.lower() for w in re.findall(r"[A-Za-z0-9\u0900-\u097F]{3,}", question)}
    ranked = []
    for s in sources:
        text = s.text or ""
        for part in chunks(text):
            low = part.lower()
            score = sum(1 for t in terms if t in low)
            if score:
                ranked.append((score, s, part))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[:12]

def gemini(prompt: str):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="AI is not configured. Add GEMINI_API_KEY to backend/.env and restart the API.")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    try:
        r = requests.post(endpoint, params={"key": API_KEY},
            json={"contents":[{"parts":[{"text":prompt}]}],
                  "generationConfig":{"temperature":0.2,"maxOutputTokens":1800}},
            timeout=45)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Gemini API error ({r.status_code}). Check your API key, quota and model configuration.")
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach the AI provider. Please retry.")


@app.post("/api/translate-ui")
def translate_ui(req: TranslateUIRequest):
    """Translate interface strings while preserving JSON keys and array structure."""
    if req.language.strip().lower() == "english":
        return {"language": "English", "strings": req.strings}
    prompt = f"""Translate the values in this UI JSON into {req.language}. Preserve every JSON key exactly, preserve array lengths and structure, and return valid JSON only. Do not translate URLs, product names such as CivicSaathi AI, or acronyms such as PM-KISAN and RTI. Use natural, clear language suitable for public-service information.
JSON:
{__import__('json').dumps(req.strings, ensure_ascii=False)}
"""
    translated = gemini(prompt)
    import json
    match = re.search(r"\{[\s\S]*\}", translated)
    if not match:
        raise HTTPException(status_code=502, detail="The translation service returned an invalid response. Please retry.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="The translation service returned invalid JSON. Please retry.")
    return {"language": req.language, "strings": data}

@app.post("/api/translate-text")
def translate_text(req: TranslateTextRequest):
    """Translate text for the voice reader without changing URLs or named entities."""
    language = req.language.strip()
    if language.lower() == "english":
        return {"language": "English", "text": req.text}
    prompt = f"""Translate the following text into natural, easy-to-understand {language} for an Indian public-service information app.
Preserve the meaning, names of government schemes, URLs, dates, amounts, and numbers. Do not add facts or explanations. Return only the translation, without quotation marks or commentary.
TEXT:
{req.text}
"""
    return {"language": language, "text": gemini(prompt).strip()}

@app.get("/api/health")
def health():
    return {"status":"ok", "ai_configured":bool(API_KEY), "model":MODEL,
            "retrieval":"live official-domain search and URL ingestion"}

@app.post("/api/search")
def search(req: SearchRequest):
    results = search_official(req.query)
    searched_query = req.query
    # Government portals are often indexed in English even when a person searches in an Indian language.
    # Best-effort query translation broadens retrieval but never blocks the original search.
    if not results and req.language.strip().lower() != "english" and API_KEY:
        try:
            translated = gemini(
                f"Translate this short search query into concise English keywords for finding Indian government schemes or public services. Preserve scheme names and acronyms. Return only the query.\nQUERY: {req.query}"
            ).strip().strip('"')
            if translated and translated.lower() != req.query.lower():
                searched_query = translated
                results = search_official(translated)
        except Exception:
            pass
    return {"query":req.query, "searched_query":searched_query, "results":results, "count":len(results),
            "fallback":not bool(results),
            "message":None if results else "We couldn't find an exact match yet. Browse the official portals below to explore related schemes and services."}

@app.post("/api/ingest-url")
def ingest(req: IngestRequest):
    try:
        return fetch_page(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/ask")
def ask(req: AskRequest):
    # Fetch supplied URLs on the server; do not trust client-supplied page text.
    # Bounded concurrency avoids making users wait for each selected source in sequence.
    def safe_fetch(source):
        try:
            return fetch_page(source.url)
        except (ValueError, requests.RequestException, OSError):
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = [item for item in pool.map(safe_fetch, req.sources[:20]) if item]
    if not fetched:
        raise HTTPException(status_code=400, detail="None of the selected official sources could be fetched. Try another official URL.")
    evidence = rank_evidence(req.question, fetched)
    if not evidence:
        # Supply bounded excerpts if lexical ranking finds no shared terms.
        evidence = [(0, s, s["text"][:1600]) for s in fetched[:4]]
    source_map, evidence_blocks = {}, []
    for _, source, passage in evidence:
        key = source["url"]
        if key not in source_map:
            source_map[key] = {"id":len(source_map)+1, "url":key, "title":source["title"]}
        sid = source_map[key]["id"]
        evidence_blocks.append(f"[SOURCE {sid}] {source['title']} | {key}\n{passage}")
    evidence_text = "\n\n".join(evidence_blocks)[:18000]
    prompt = f"""
You are CivicSaathi AI, a careful public-information explainer for India.
Answer in {req.language}. User question: {req.question}

Use ONLY the supplied official-source evidence for factual claims. Do not invent scheme rules, amounts, dates, eligibility, document lists, legal interpretations or links.
Clearly separate: (1) what the source explicitly says, (2) what is not stated/unclear.
If the evidence is insufficient, say so prominently and tell the user which official office/portal to check.
Never claim to submit an application or guarantee eligibility. Do not request Aadhaar numbers, bank details, passwords or other sensitive identifiers.
Use plain, accessible language and always include these headings:
## Simple explanation
## Eligibility criteria
List each criterion supported by the sources. Distinguish confirmed criteria from missing/unclear criteria. Do not say the user is personally eligible or ineligible unless enough user details were supplied and the official criteria support that conclusion.
## Benefits
List benefits or rights explicitly supported by the sources.
## Documents required
List only documents explicitly mentioned as required; label other documents as unconfirmed.
## How to apply / next steps
Give concrete steps and official links where the sources support them.
## What I need to assess your eligibility
Ask only for non-sensitive details that materially affect eligibility (for example age range, state, occupation, income bracket, farmer/student status). Never ask for Aadhaar, bank account numbers, passwords, OTPs or full identity numbers.
## Important caveats
Cite factual paragraphs using [Source 1], [Source 2], etc. Use only the source numbers below.
Do not present a document as mandatory unless the source says so.

OFFICIAL SOURCE EVIDENCE:
{evidence_text}

Return a useful, concise answer. Do not output fabricated citations.
"""
    answer = gemini(prompt)
    # Second-pass grounding audit: asks model to flag claims not supported by evidence.
    audit_prompt = f"""Review this civic-information answer against the evidence. Do not rewrite it.
Return exactly one of: GROUNDED, CHECK SOURCES, or INSUFFICIENT EVIDENCE.
Be conservative: if a concrete rule, date, amount or eligibility claim lacks clear support, choose CHECK SOURCES.
ANSWER:
{answer[:7000]}
EVIDENCE:
{evidence_text[:12000]}"""
    audit = gemini(audit_prompt).strip().splitlines()[0][:80]
    return {"answer":answer, "sources":list(source_map.values()),
            "grounding_status":audit, "language":req.language,
            "disclaimer":"Informational aid only, not legal advice or an official government service. Verify important details with the linked authority."}

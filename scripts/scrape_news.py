from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
import time
import os
import pandas as pd
from sqlalchemy import create_engine, text
import re

DB_URL = os.getenv("DB_URL")
if not DB_URL:
    raise Exception("Set DB_URL as env var first")
engine = create_engine(DB_URL, connect_args={"connect_timeout": 10})

MIN_ARTICLES = 18

# 🎯 Finance/Stock Keywords
FINANCE_KEYWORDS = [
    "saham", "ihsg", "idx", "bursa", "bei", "idx composite", "bursa efek", "emiten",
    "investor", "manajer investasi", "analis pasar", "trader", "institusi", "asing",
    "domestik", "ritel", "dividen", "obligasi", "reksadana", "warrant", "rights issue",
    "saham preferen", "saham biasa", "sekuritas", "ipo", "listing", "delisting",
    "go public", "pencatatan saham", "suspensi", "auto reject", "aksi korporasi",
    "stock split", "reverse stock", "buyback", "right issue", "private placement",
    "kapitalisasi pasar", "volume perdagangan", "frekuensi perdagangan", "indeks",
    "lq45", "idx30", "kompas100", "issi", "jii", "market cap", "free float",
    "bullish", "bearish", "rally", "koreksi", "rebound", "volatile", "sideways",
    "breakout", "resistance", "support", "inflasi", "suku bunga", "bi rate",
    "bi 7drr", "fed rate", "kurs", "rupiah", "nilai tukar", "cadangan devisa",
    "pertumbuhan ekonomi", "gdp", "pdb", "neraca perdagangan", "tingkat pengangguran",
    "indeks keyakinan konsumen", "pmi", "perbankan", "properti", "tambang", "energi",
    "konsumer", "teknologi", "telekomunikasi", "infrastruktur", "transportasi",
    "industri dasar", "aneka industri", "barang konsumsi", "rekomendasi", "target harga",
    "valuasi", "pe ratio", "pbv", "eps", "roe", "fundamental", "teknikal",
    "sentimen pasar", "net buy", "net sell", "foreign flow", "asing beli", "asing jual",
    "akumulasi", "distribusi", "profit taking", "window dressing", "bursa regional",
    "ojk", "apbn", "kebijakan moneter", "pailit", "gagal bayar", "pkpu", "tender offer",
    "akuisisi", "merger", "laporan keuangan", "kinerja emiten", "laba bersih",
    "rugi bersih", "bank indonesia", "liabilitas", "perdagangan saham", "penutupan ihsg",
    "pembukaan ihsg", "indeks harga saham gabungan", "indeks saham"
]

# Compile regex pattern for efficient matching (case-insensitive)
# Using word boundaries to avoid partial matches
KEYWORD_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(kw) for kw in FINANCE_KEYWORDS) + r')\b',
    re.IGNORECASE
)

def is_finance_related(text):
    if not text or str(text).strip() == "":
        return False
    return bool(KEYWORD_PATTERN.search(str(text)))

def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(180)
    return driver

def format_detik_date(date_obj):
    return date_obj.strftime("%m/%d/%Y")

def scrape_article(url, kategori, fallback_date):
    """
    Scrape article and validate if it's finance-related.
    Returns article dict if valid, None otherwise.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=14)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Extract title
        judul_el = soup.find("h1", class_="detail__title")
        judul = judul_el.get_text(strip=True) if judul_el else ""
        
        # Extract date
        tanggal_element = soup.find("div", class_="date")
        if tanggal_element:
            try:
                date_str = tanggal_element.get_text(strip=True).split(",")[1].strip()
                date_obj = datetime.strptime(date_str, "%d %b %Y")
                tanggal = date_obj.strftime("%Y-%m-%d")
            except:
                tanggal = fallback_date.strftime("%Y-%m-%d")
        else:
            tanggal = fallback_date.strftime("%Y-%m-%d")
        
        # Extract content
        paragraphs = soup.select("div.detail__body-text.itp_bodycontent > p")
        konten = "\n".join([p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)])
        
        # Check title first (most efficient)
        title_match = is_finance_related(judul)
        content_match = False
        if not title_match and konten:
            content_match = is_finance_related(konten)
        if not (title_match or content_match):
            return None
        
        return {
            "tanggal": tanggal, 
            "kategori": kategori, 
            "judul": judul, 
            "url": url, 
            "konten": konten
        }
    except Exception as e:
        return None

def insert_raw_news(row):
    sql = text("""
        INSERT INTO raw_news (tanggal, kategori, judul, url, konten, scraped_at)
        VALUES (:tanggal, :kategori, :judul, :url, :konten, now())
        ON CONFLICT (url) DO NOTHING
        RETURNING id;""")
    with engine.begin() as conn:
        try:
            res = conn.execute(sql, {
                "tanggal": row["tanggal"],
                "kategori": row["kategori"],
                "judul": row["judul"],
                "url": row["url"],
                "konten": row["konten"]
            })
            return res.fetchone() is not None
        except:
            return False

def count_articles_for_date(date_str):
    sql = text("SELECT COUNT(*) FROM raw_news WHERE tanggal = :tanggal")
    with engine.connect() as conn:
        result = conn.execute(sql, {"tanggal": date_str}).scalar()
    return result or 0

def scrape_page(driver, url, kategori, current_date):
    try:
        driver.get(url)
        time.sleep(2)
        artikel_elements = driver.find_elements(By.CSS_SELECTOR, ".list-content__item")
        
        if not artikel_elements:
            return {"inserted": 0, "discarded": 0}
        
        # Extract all article links first
        artikel_links = []
        for artikel in artikel_elements:
            try:
                link = artikel.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
                artikel_links.append(link)
            except:
                continue
        
        inserted_count = 0
        discarded_count = 0
        
        # Process each article with keyword filtering
        for link in artikel_links:
            art = scrape_article(link, kategori, current_date)
            if art and art.get("konten"):
                # Article passed keyword filter
                if insert_raw_news(art):
                    inserted_count += 1
            else:
                # Article was discarded (no keyword match or scraping failed)
                discarded_count += 1
        
        return {"inserted": inserted_count, "discarded": discarded_count}
    except:
        return {"inserted": 0, "discarded": 0}

def run_scrape(start_date, end_date):
    driver = create_driver()
    kategori = "ekonomi"
    current = start_date
    
    print(f"\nKeyword Filter Active: {len(FINANCE_KEYWORDS)} keywords loaded")
    total_stats = {"inserted": 0, "discarded": 0, "days_processed": 0}
    
    while current <= end_date:
        tanggal_str = format_detik_date(current)
        date_str_db = current.strftime("%Y-%m-%d")
        
        print(f"📅 {tanggal_str}", end=" ")
        
        existing_count = count_articles_for_date(date_str_db)
        
        if existing_count >= MIN_ARTICLES:
            print(f"{existing_count} artikel")
            current += timedelta(days=1)
            continue
        
        page = 1
        total_new = 0
        total_discarded = 0
        max_pages = 10  # Safety limit to prevent infinite loops
        
        # Keep scraping until we hit MIN_ARTICLES or run out of pages
        while existing_count + total_new < MIN_ARTICLES and page <= max_pages:
            url = f"https://finance.detik.com/indeks?page={page}&date={tanggal_str}"
            stats = scrape_page(driver, url, kategori, current)
            
            if stats["inserted"] == 0 and stats["discarded"] == 0:
                # No more articles available
                break
            
            total_new += stats["inserted"]
            total_discarded += stats["discarded"]
            page += 1
            time.sleep(1)
        
        final_count = existing_count + total_new
        
        # Status output with color coding
        if final_count >= MIN_ARTICLES:
            status = "✅"
        elif final_count > 0:
            status = "⚠️"
        else:
            status = "❌"
        
        print(f"{status} {final_count} artikel (+{total_new} baru, -{total_discarded} filtered)")
        
        total_stats["inserted"] += total_new
        total_stats["discarded"] += total_discarded
        total_stats["days_processed"] += 1
        
        current += timedelta(days=1)
    
    driver.quit()
    
    # Summary statistics
    print("SCRAPING SUMMARY")
    print(f"Days processed: {total_stats['days_processed']}")
    print(f"Articles inserted: {total_stats['inserted']}")
    print(f"Articles filtered out: {total_stats['discarded']}")
    if total_stats['inserted'] + total_stats['discarded'] > 0:
        acceptance_rate = total_stats['inserted'] / (total_stats['inserted'] + total_stats['discarded']) * 100
        print(f"Acceptance rate: {acceptance_rate:.1f}%")
    print("="*60)
    print("\nDone")

def get_last_scraped_date():
    sql = text("SELECT MAX(tanggal) FROM raw_news;")
    with engine.connect() as conn:
        result = conn.execute(sql).scalar()
    return result

def get_scrape_range(lookback_days=3):
    today = datetime.now(timezone(timedelta(hours=7))).date()
    last_date = get_last_scraped_date()
    
    if last_date is None:
        start_date = today - timedelta(days=3)
    else:
        lookback_date = today - timedelta(days=lookback_days)
        start_date = max(lookback_date, last_date - timedelta(days=lookback_days))
    
    end_date = today
    
    if start_date > end_date:
        return None, None
    return start_date, end_date

if __name__ == "__main__":
    LOOKBACK_DAYS = 3 
    start_date, end_date = get_scrape_range(lookback_days=LOOKBACK_DAYS)
    
    if not start_date:
        print("No new dates to scrape")
    else:
        print(f"Scraping: {start_date} → {end_date} (validasi {LOOKBACK_DAYS} hari)")
        run_scrape(
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date, datetime.min.time())
        )
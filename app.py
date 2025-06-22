from flask import Flask, request, Response
from flask_cors import CORS
import threading
import json
import os
import time
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import concurrent.futures
from urllib.parse import urlparse

app = Flask(__name__)
CORS(app, resources={r"/get_results": {"origins": "*"}})

CUR_STREAM = {
    'cancel_event': None,
    'lock': threading.Lock()
}

# Enhanced Chrome setup with aggressive memory optimization
def create_driver():
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/chromium-browser"

    # Aggressive performance optimizations
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--js-flags=--max_old_space_size=50")  # Reduced from 100
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--disable-images")
    chrome_options.add_argument("--disable-javascript")  # Major speedup if JS not needed
    chrome_options.add_argument("--disable-css")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_argument("--hide-scrollbars")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--window-size=400x300")  # Smaller window
    chrome_options.page_load_strategy = 'none'  # Don't wait for full page load

    # Prefs for additional speed
    prefs = {
        "profile.default_content_setting_values": {
            "images": 2,  # Block images
            "plugins": 2,  # Block plugins
            "popups": 2,  # Block popups
            "geolocation": 2,  # Block location sharing
            "notifications": 2,  # Block notifications
            "media_stream": 2,  # Block media stream
        },
        "profile.managed_default_content_settings": {
            "images": 2
        }
    }
    chrome_options.add_experimental_option("prefs", prefs)

    service = Service('/usr/lib/chromium-browser/chromedriver')
    return webdriver.Chrome(service=service, options=chrome_options)

# Parallel link resolution
def resolve_bing_links_parallel(driver, links):
    """Resolve multiple Bing redirect links in parallel using tabs"""
    clean_links = []
    
    # Open all links in separate tabs first
    for link in links:
        driver.execute_script("window.open(arguments[0]);", link)
    
    # Now resolve all the URLs
    for i, link in enumerate(links, 1):  # Start from 1 since 0 is the original tab
        try:
            driver.switch_to.window(driver.window_handles[i])
            time.sleep(0.5)  # Minimal wait
            clean_links.append(driver.current_url)
        except:
            clean_links.append(link)  # Fallback to original if error
    
    # Close all tabs except the first one
    while len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
        driver.close()
    
    driver.switch_to.window(driver.window_handles[0])
    return clean_links

# Optimized text extraction with early termination
def extract_clean_text_fast(driver, sentence_cleaned, max_chars=4000):
    """Extract text with early termination if sentence found"""
    try:
        # Use faster selector and limit elements
        elements = driver.find_elements(By.CSS_SELECTOR, "p, blockquote, li")[:20]  # Limit to first 20 elements
        
        text_parts = []
        current_length = 0
        
        for el in elements:
            if current_length >= max_chars:
                break
                
            text = el.text.strip()
            if text:
                # Check if we found the sentence we're looking for
                if sentence_cleaned in text:
                    return ""  # Return empty if sentence found (duplicate content)
                
                text_parts.append(text)
                current_length += len(text)
        
        return " ".join(text_parts)[:max_chars]
    except:
        return ""

# Enhanced lightweight scraping with better error handling
def scrape_with_requests_fast(url, sentence_cleaned, timeout=5):
    """Faster requests-based scraping with shorter timeout"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
        
        # Read only first chunk for speed
        content = ""
        for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
            content += chunk
            if len(content) > 50000:  # Stop after 50KB
                break
        
        soup = BeautifulSoup(content, 'html.parser')
        
        # Remove unwanted elements for speed
        for element in soup(['script', 'style', 'nav', 'footer', 'header']):
            element.decompose()
        
        # Fast text extraction
        text_elements = soup.find_all(['p', 'blockquote', 'li'], limit=15)  # Limit elements
        text_parts = []
        
        for elem in text_elements:
            text = elem.get_text(strip=True)
            if text and len(text) > 20:  # Skip very short texts
                if sentence_cleaned in text:
                    return {"clean_link": "", "text_cleaned": ""}  # Skip if duplicate
                text_parts.append(text)
        
        combined_text = '\n'.join(text_parts)[:4000]
        
        return {"clean_link": url, "text_cleaned": combined_text} if combined_text else {"clean_link": "", "text_cleaned": ""}
        
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return {"clean_link": "", "text_cleaned": ""}

# Parallel processing of multiple URLs
def process_urls_parallel(urls, sentence_cleaned, use_lightweight=True, max_workers=3):
    """Process multiple URLs in parallel"""
    results = []
    
    if use_lightweight:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {
                executor.submit(scrape_with_requests_fast, url, sentence_cleaned): url 
                for url in urls
            }
            
            for future in concurrent.futures.as_completed(future_to_url, timeout=10):
                try:
                    result = future.result(timeout=5)
                    results.append(result)
                except:
                    results.append({"clean_link": "", "text_cleaned": ""})
    
    return results

@app.route('/get_results')
def get_results():
    global CUR_STREAM

    with CUR_STREAM['lock']:
        if CUR_STREAM['cancel_event'] is not None:
            CUR_STREAM['cancel_event'].set()
        cancel_event = threading.Event()
        CUR_STREAM['cancel_event'] = cancel_event

    query = request.args.get('query')
    sentence = request.args.get('sentence')
    starting_index = int(request.args.get('starting_index', 0))
    sentence_cleaned = " ".join(sentence.split())
    use_lightweight = request.args.get('lightweight', 'true').lower() == 'true'

    def generate(cancel_event):
        DRIVER = None
        try:
            DRIVER = create_driver()
            DRIVER.set_page_load_timeout(5)  # Reduced timeout
            
            # Faster Bing search
            DRIVER.get("https://www.bing.com/search?q=" + query.replace(' ', '+'))
            time.sleep(1)  # Reduced wait time

            # Skip cookie banners entirely or handle very quickly
            try:
                WebDriverWait(DRIVER, 0.5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reject')]"))
                ).click()
            except:
                pass

            # Get search results faster
            results = DRIVER.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")
            if not results:
                # Fallback selector
                results = DRIVER.find_elements(By.CSS_SELECTOR, "h2 a")
            
            links = [el.get_attribute("href") for el in results[starting_index:starting_index + 5]]
            
            # Resolve links in parallel if using Selenium
            if not use_lightweight:
                links = resolve_bing_links_parallel(DRIVER, links)
            
            # Close driver if using lightweight mode
            if use_lightweight and DRIVER:
                DRIVER.quit()
                DRIVER = None
            
            print(f"Processing {len(links)} links")
            
            if use_lightweight:
                # Process all URLs in parallel for maximum speed
                results = process_urls_parallel(links, sentence_cleaned)
                for result in results:
                    if cancel_event.is_set():
                        break
                    yield "data: " + json.dumps(result) + "\n\n"
            else:
                # Sequential processing for Selenium mode
                for link in links:
                    if cancel_event.is_set():
                        break
                    
                    yield "data: PROCESSING\n\n"
                    
                    try:
                        # Quick page load with minimal waiting
                        DRIVER.get(link)
                        
                        # Skip cookie acceptance for speed
                        text = extract_clean_text_fast(DRIVER, sentence_cleaned)
                        
                        result = {
                            "clean_link": DRIVER.current_url if text else "",
                            "text_cleaned": text
                        }
                        yield "data: " + json.dumps(result) + "\n\n"
                        
                    except Exception as e:
                        print(f"Error processing {link}: {e}")
                        yield "data: " + json.dumps({"clean_link": "", "text_cleaned": ""}) + "\n\n"

            yield "data: END\n\n"

        except Exception as e:
            print(f"Fatal error: {e}")
            yield "data: ERROR\n\n"
        finally:
            if DRIVER:
                DRIVER.quit()

    return Response(generate(cancel_event), mimetype='text/event-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

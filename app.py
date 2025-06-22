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
from urllib.parse import urljoin, urlparse


app = Flask(__name__)
CORS(app, resources={r"/get_results": {"origins": "*"}})

CUR_STREAM = {
    'cancel_event': None,
    'lock': threading.Lock()
}

# Reuse session for requests
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
})

# Chrome setup with memory optimization
def create_driver():
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/chromium-browser"  # or /usr/bin/chromium

    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--js-flags=--max_old_space_size=100")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument("--window-size=800x600")
    chrome_options.add_argument("--disable-javascript")  # Disable JS for faster loading
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.page_load_strategy = 'eager'

    service = Service('/usr/lib/chromium-browser/chromedriver')

    # Tell Selenium this is Chromium, not Chrome
    return webdriver.Chrome(service=service, options=chrome_options)

def get_clean_bing_links_batch(driver, links):
    """Process multiple links more efficiently"""
    clean_links = []
    original_window = driver.current_window_handle
    
    for link in links:
        try:
            driver.execute_script("window.open(arguments[0]);", link)
            driver.switch_to.window(driver.window_handles[-1])
            time.sleep(1)  # Reduced from 3 seconds
            clean_link = driver.current_url
            driver.close()
            clean_links.append(clean_link)
        except Exception as e:
            print(f"Error resolving link {link}: {e}")
            clean_links.append(link)  # Fallback to original link
        finally:
            if len(driver.window_handles) > 1:
                driver.switch_to.window(original_window)
    
    return clean_links

# Faster text extraction with single query
def extract_clean_text(driver):
    try:
        # Single query for all elements
        elements = driver.find_elements(By.CSS_SELECTOR, "p, blockquote, li")
        combined = " ".join(el.text.strip() for el in elements if el.text.strip())
        return combined[:4000]
    except Exception as e:
        print(f"Error extracting text: {e}")
        return ""

# Optimized BeautifulSoup scraping
def scrape_with_requests(url, sentence_cleaned):
    try:
        response = session.get(url, timeout=5, stream=True)  # Reduced timeout, use streaming
        
        # Only read what we need
        content = ""
        for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
            content += chunk
            if len(content) > 50000:  # Stop reading after reasonable amount
                break
        
        soup = BeautifulSoup(content, 'lxml')  # lxml is faster than html.parser
        
        # More efficient text extraction
        text_elements = []
        for tag in soup.find_all(['p', 'blockquote', 'li']):
            # Replace <br> tags with newlines in one go
            for br in tag.find_all("br"):
                br.replace_with("\n")
            
            text = tag.get_text(separator='\n', strip=True)
            if text:
                text_elements.append(text)

        text = '\n'.join(text_elements)[:4000]

        print("TEEEEXT")
        print(text[:600])
        
        if text and sentence_cleaned not in text:
            return {"clean_link": url, "text_cleaned": text}
        return {"clean_link": "", "text_cleaned": ""}
    except Exception as e:
        print(f"Error scraping {url} with requests: {e}")
        return {"clean_link": "", "text_cleaned": ""}

def scrape_single_link(args):
    """Helper function for parallel scraping"""
    link, sentence_cleaned, use_lightweight, cancel_event = args
    
    if cancel_event.is_set():
        return {"clean_link": "", "text_cleaned": ""}
    
    try:
        if use_lightweight:
            return scrape_with_requests(link, sentence_cleaned)
        else:
            # For selenium mode, we'll still process sequentially to avoid driver conflicts
            return {"clean_link": link, "text_cleaned": "selenium_fallback"}
    except Exception as e:
        print(f"Error scraping {link}: {e}")
        return {"clean_link": "", "text_cleaned": ""}

@app.route('/get_results')
def get_results():
    global CUR_STREAM

    with CUR_STREAM['lock']:
        # Cancel previous stream if any
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
        print("here")
        DRIVER = None
        try:
            DRIVER = create_driver()
            DRIVER.set_page_load_timeout(10)  # Reduced from 15
            
            DRIVER.get("https://www.bing.com/")
            time.sleep(1)  # Reduced wait time

            print(DRIVER.page_source[:2000])

            # Faster cookie/popup handling
            try:
                reject_btn = WebDriverWait(DRIVER, 0.5).until(  # Reduced wait time
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reject') or contains(text(), 'Decline')]"))
                )
                reject_btn.click()
                time.sleep(0.5)  # Reduced wait time
            except:
                pass 

            search_box = DRIVER.find_element(By.NAME, "q")
            search_box.clear()
            search_box.send_keys(query)
            search_box.send_keys(Keys.RETURN)
            time.sleep(0.5)  # Reduced wait time

            # Wait for results to load
            WebDriverWait(DRIVER, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "li.b_algo h2 a"))
            )

            results = DRIVER.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")
            print("RESULTS")
            print(results)
            
            raw_links = [el.get_attribute("href") for el in results[starting_index:starting_index + 5]]
            links = get_clean_bing_links_batch(DRIVER, raw_links)  # Batch processing
            
            print("LINKS")
            print(links)
            
            if use_lightweight:
                # Parallel processing for lightweight mode
                if DRIVER:
                    DRIVER.quit()
                    DRIVER = None
                
                # Process links in parallel
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    scrape_args = [(link, sentence_cleaned, use_lightweight, cancel_event) for link in links]
                    future_to_link = {executor.submit(scrape_single_link, args): args[0] for args in scrape_args}
                    
                    for future in concurrent.futures.as_completed(future_to_link):
                        if cancel_event.is_set():
                            break
                        
                        link = future_to_link[future]
                        yield "data: PROCESSING\n\n"
                        
                        try:
                            result = future.result(timeout=10)
                            yield json.dumps(result) + "\n"
                        except Exception as e:
                            print(f"Error processing {link}: {e}")
                            yield json.dumps({"clean_link": "", "text_cleaned": ""}) + "\n"
            else:
                # Sequential processing for selenium mode (to avoid driver conflicts)
                for link in links:
                    print("LINK")
                    print(link)

                    if cancel_event.is_set():
                        break
                    
                    yield "data: PROCESSING\n\n"
                    
                    try:
                        if DRIVER:
                            # Clear cache more efficiently
                            DRIVER.execute_script("""
                                window.localStorage.clear();
                                window.sessionStorage.clear();
                            """)
                            DRIVER.delete_all_cookies()
                            
                            DRIVER.get(link)
                            time.sleep(0.5)  # Reduced wait time

                            # Faster popup handling
                            try:
                                accept_btn = WebDriverWait(DRIVER, 1).until(  # Reduced wait time
                                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept all') or contains(text(), 'Accept')]"))
                                )
                                accept_btn.click()
                                time.sleep(0.5)  # Reduced wait time
                            except:
                                pass

                            text = extract_clean_text(DRIVER)

                            if text and sentence_cleaned not in text:
                                yield json.dumps({"clean_link": DRIVER.current_url, "text_cleaned": text}) + "\n"
                            else:
                                yield json.dumps({"clean_link": "", "text_cleaned": ""}) + "\n"
                
                    except TimeoutException:
                        print(f"Timeout for {link}")
                        yield json.dumps({"clean_link": "", "text_cleaned": "Page load timeout"}) + "\n"
                    except Exception as e:
                        print(f"Error scraping {link}: {e}")
                        yield json.dumps({"clean_link": "", "text_cleaned": ""}) + "\n"

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

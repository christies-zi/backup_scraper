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
import queue
import atexit

app = Flask(__name__)
CORS(app, resources={r"/get_results": {"origins": "*"}})

# Configuration
POOL_SIZE = 3  # Number of drivers to maintain in pool
MAX_DRIVER_USAGE = 50  # Restart driver after this many uses to prevent memory leaks

# Driver pool and management
DRIVER_POOL = queue.Queue(maxsize=POOL_SIZE)
DRIVER_USAGE_COUNT = {}
POOL_LOCK = threading.Lock()
POOL_INITIALIZED = False  # Add this flag

# Semaphore to limit concurrent requests
REQUEST_SEMAPHORE = threading.Semaphore(5)

# Dictionary to track active streams by thread ID
ACTIVE_STREAMS = {}
STREAMS_LOCK = threading.Lock()

class DriverWrapper:
    def __init__(self, driver, driver_id):
        self.driver = driver
        self.driver_id = driver_id
        self.usage_count = 0
        self.lock = threading.Lock()  # Each driver gets its own lock
    
    def use(self):
        with self.lock:
            self.usage_count += 1
            return self.usage_count

def create_driver():
    """Create a new Chrome driver instance"""
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/chromium-browser"

    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--js-flags=--max_old_space_size=100")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument("--window-size=800x600")
    chrome_options.page_load_strategy = 'eager'

    service = Service('/usr/lib/chromium-browser/chromedriver')
    return webdriver.Chrome(service=service, options=chrome_options)

def initialize_driver_pool():
    """Initialize the driver pool at app startup"""
    global POOL_INITIALIZED
    
    with POOL_LOCK:
        if POOL_INITIALIZED:
            return  # Already initialized
        
        print("Initializing driver pool...")
        for i in range(POOL_SIZE):
            try:
                driver = create_driver()
                wrapper = DriverWrapper(driver, f"driver_{i}")
                DRIVER_POOL.put(wrapper)
                DRIVER_USAGE_COUNT[wrapper.driver_id] = 0
                print(f"Driver {i+1}/{POOL_SIZE} initialized")
            except Exception as e:
                print(f"Failed to initialize driver {i}: {e}")
        
        POOL_INITIALIZED = True
        print("Driver pool initialization complete")

def get_driver():
    """Get a driver from the pool"""
    try:
        wrapper = DRIVER_POOL.get(timeout=30)  # Wait up to 30 seconds
        usage_count = wrapper.use()
        
        # If driver has been used too many times, restart it
        if usage_count > MAX_DRIVER_USAGE:
            print(f"Restarting driver {wrapper.driver_id} after {usage_count} uses")
            try:
                wrapper.driver.quit()
            except:
                pass
            
            new_driver = create_driver()
            wrapper.driver = new_driver
            wrapper.usage_count = 0
        
        return wrapper
    except queue.Empty:
        print("No drivers available in pool, creating temporary driver")
        # Fallback: create temporary driver if pool is exhausted
        driver = create_driver()
        return DriverWrapper(driver, "temp")

def return_driver(wrapper):
    """Return a driver to the pool"""
    if wrapper.driver_id == "temp":
        # Don't return temporary drivers to pool
        try:
            wrapper.driver.quit()
        except:
            pass
    else:
        try:
            # Clean up driver state before returning to pool
            wrapper.driver.execute_script("window.localStorage.clear();")
            wrapper.driver.execute_script("window.sessionStorage.clear();")
            wrapper.driver.delete_all_cookies()
            DRIVER_POOL.put(wrapper, timeout=1)
        except Exception as e:
            print(f"Error returning driver to pool: {e}")
            # If we can't return it, quit and create a new one
            try:
                wrapper.driver.quit()
            except:
                pass
            try:
                new_driver = create_driver()
                new_wrapper = DriverWrapper(new_driver, wrapper.driver_id)
                DRIVER_POOL.put(new_wrapper, timeout=1)
            except Exception as e2:
                print(f"Error creating replacement driver: {e2}")

def cleanup_drivers():
    """Clean up all drivers in the pool"""
    print("Cleaning up driver pool...")
    while not DRIVER_POOL.empty():
        try:
            wrapper = DRIVER_POOL.get_nowait()
            wrapper.driver.quit()
        except:
            pass

def get_clean_bing_links(driver, link):
    driver.execute_script("window.open(arguments[0]);", link)
    driver.switch_to.window(driver.window_handles[-1])
    time.sleep(3)
    clean_link = driver.current_url
    driver.close()
    driver.switch_to.window(driver.window_handles[0])
    return clean_link

def extract_clean_text(driver):
    elements = driver.find_elements(By.CSS_SELECTOR, "p, blockquote")
    list_elements = driver.find_elements(By.CSS_SELECTOR, "li")  # Fixed: was find_element
    combined = " ".join(el.text.strip() for el in elements + list_elements if el.text.strip())
    return combined[:4000]

def scrape_with_requests(url, sentence_cleaned):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        paragraphs = soup.find_all(['p', 'blockquote']) 
        less_important_stuff = soup.find_all(['li', 'blockquote'])
        elements_split = []

        for elem in paragraphs + less_important_stuff:
            for br in elem.find_all("br"):
                br.replace_with("\n")
            
            lines = elem.text.split('\n')
            clean_lines = '\n'.join([line.strip() for line in lines if line.strip()])
            elements_split.append(clean_lines)

        text = '\n'.join([e.strip() for e in elements_split if e.strip()])[:4000]

        print("TEXT FROM REQUESTS:")
        print(text[:600])
        
        if text and sentence_cleaned not in text:
            return {"clean_link": url, "text_cleaned": text}
        return {"clean_link": "", "text_cleaned": ""}
    except Exception as e:
        print(f"Error scraping {url} with requests: {e}")
        return {"clean_link": "", "text_cleaned": ""}

@app.route('/get_results')
def get_results():
    # Initialize pool if not already done (lazy initialization)
    if not POOL_INITIALIZED:
        initialize_driver_pool()
    
    query = request.args.get('query')
    sentence = request.args.get('sentence')
    starting_index = int(request.args.get('starting_index', 0))
    sentence_cleaned = " ".join(sentence.split())
    
    use_lightweight = request.args.get('lightweight', 'true').lower() == 'true'

    def generate():
        thread_id = threading.get_ident()
        cancel_event = threading.Event()
        
        with STREAMS_LOCK:
            ACTIVE_STREAMS[thread_id] = cancel_event
        
        REQUEST_SEMAPHORE.acquire()
        
        driver_wrapper = None
        try:
            print("Getting driver from pool...")
            driver_wrapper = get_driver()
            driver = driver_wrapper.driver
            
            print(f"Using driver {driver_wrapper.driver_id} (usage: {driver_wrapper.usage_count})")
            
            try:
                driver.set_page_load_timeout(15)
                driver.get("https://www.bing.com/")
                time.sleep(2)

                print("Page loaded, looking for reject button...")

                try:
                    reject_btn = WebDriverWait(driver, 1).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reject') or contains(text(), 'Decline')]"))
                    )
                    reject_btn.click()
                    time.sleep(1)
                except:
                    pass

                search_box = driver.find_element(By.NAME, "q")
                search_box.send_keys(query)
                search_box.send_keys(Keys.RETURN)
                time.sleep(1)

                results = driver.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")
                print(f"Found {len(results)} results")
                
                links = [el.get_attribute("href") for el in results[starting_index:starting_index + 5]]
                links = [get_clean_bing_links(driver, link) for link in links]
                
                # If using lightweight mode, we can return the driver early
                if use_lightweight:
                    return_driver(driver_wrapper)
                    driver_wrapper = None
                
                print(f"Processing {len(links)} links")
                for link in links:
                    if cancel_event.is_set():
                        break
                    
                    yield "data: PROCESSING\n\n"
                    
                    try:
                        if use_lightweight:
                            result = scrape_with_requests(link, sentence_cleaned)
                            yield json.dumps(result) + "\n"
                        else:
                            if driver_wrapper and driver_wrapper.driver:
                                driver.execute_script("window.localStorage.clear();")
                                driver.execute_script("window.sessionStorage.clear();")
                                driver.delete_all_cookies()
                                
                                driver.get(link)
                                time.sleep(1)

                                try:
                                    accept_btn = WebDriverWait(driver, 2).until(
                                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept all') or contains(text(), 'Accept')]"))
                                    )
                                    accept_btn.click()
                                    time.sleep(1)
                                except:
                                    pass

                                text = extract_clean_text(driver)

                                if text and sentence_cleaned not in text:
                                    yield json.dumps({"clean_link": driver.current_url, "text_cleaned": text}) + "\n"
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
            # Return driver to pool if we still have it
            if driver_wrapper:
                return_driver(driver_wrapper)
            
            with STREAMS_LOCK:
                ACTIVE_STREAMS.pop(thread_id, None)
            REQUEST_SEMAPHORE.release()

    return Response(generate(), mimetype='text/event-stream')

# Register cleanup function
atexit.register(cleanup_drivers)

# Health check endpoint
@app.route('/health')
def health_check():
    return {"status": "ok", "pool_initialized": POOL_INITIALIZED}

if __name__ == '__main__':
    # Initialize pool before starting the server
    initialize_driver_pool()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

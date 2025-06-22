from flask import Flask, request, Response
from flask_cors import CORS
import threading
import json
import os
import time
import requests
import psutil
import signal
import gc
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

# Configuration - Reduced for better memory management
POOL_SIZE = 2  # Reduced from 3
MAX_DRIVER_USAGE = 10  # Significantly reduced from 50

# Driver pool and management
DRIVER_POOL = queue.Queue(maxsize=POOL_SIZE)
DRIVER_USAGE_COUNT = {}
POOL_LOCK = threading.Lock()
POOL_INITIALIZED = False

# Reduced semaphore to limit concurrent requests
REQUEST_SEMAPHORE = threading.Semaphore(2)  # Reduced from 5

# Dictionary to track active streams by thread ID
ACTIVE_STREAMS = {}
STREAMS_LOCK = threading.Lock()

class DriverWrapper:
    def __init__(self, driver, driver_id):
        self.driver = driver
        self.driver_id = driver_id
        self.usage_count = 0
        self.lock = threading.Lock()
    
    def use(self):
        with self.lock:
            self.usage_count += 1
            return self.usage_count

def log_memory_usage():
    """Log current memory usage"""
    try:
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024
        print(f"Memory usage: {memory_mb:.1f}MB")
        return memory_mb
    except:
        return 0

def kill_chrome_processes():
    """Kill any lingering Chrome processes"""
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            if proc.info['name'] and ('chrome' in proc.info['name'].lower() or 'chromium' in proc.info['name'].lower()):
                try:
                    proc.kill()
                    print(f"Killed lingering Chrome process: {proc.info['pid']}")
                except:
                    pass
    except Exception as e:
        print(f"Error killing Chrome processes: {e}")

def create_driver():
    """Create a new Chrome driver instance with aggressive memory management"""
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/chromium-browser"

    # Core headless options
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument("--window-size=800x600")
    
    # CRITICAL memory management flags
    chrome_options.add_argument("--memory-pressure-off")
    chrome_options.add_argument("--max_old_space_size=256")
    chrome_options.add_argument("--aggressive-cache-discard")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
    chrome_options.add_argument("--disable-ipc-flooding-protection")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--disable-component-extensions-with-background-pages")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    
    chrome_options.page_load_strategy = 'eager'

    service = Service('/usr/lib/chromium-browser/chromedriver')
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    # Set aggressive timeouts
    driver.set_page_load_timeout(10)
    driver.implicitly_wait(3)
    
    return driver

def force_cleanup_driver(wrapper):
    """Aggressively clean up a driver instance"""
    try:
        # Try graceful shutdown first
        wrapper.driver.quit()
    except:
        pass
    
    # Force kill any remaining processes
    try:
        if hasattr(wrapper.driver, 'service') and hasattr(wrapper.driver.service, 'process'):
            pid = wrapper.driver.service.process.pid
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                os.kill(pid, signal.SIGKILL)
            except:
                pass
    except:
        pass

def initialize_driver_pool():
    """Initialize the driver pool at app startup"""
    global POOL_INITIALIZED
    
    with POOL_LOCK:
        if POOL_INITIALIZED:
            return
        
        print("Initializing driver pool...")
        log_memory_usage()
        
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
        log_memory_usage()

def get_driver():
    """Get a driver from the pool"""
    try:
        wrapper = DRIVER_POOL.get(timeout=30)
        usage_count = wrapper.use()
        
        # If driver has been used too many times, restart it
        if usage_count > MAX_DRIVER_USAGE:
            print(f"Restarting driver {wrapper.driver_id} after {usage_count} uses")
            force_cleanup_driver(wrapper)
            
            new_driver = create_driver()
            wrapper.driver = new_driver
            wrapper.usage_count = 0
        
        return wrapper
    except queue.Empty:
        print("No drivers available in pool, creating temporary driver")
        driver = create_driver()
        return DriverWrapper(driver, "temp")

def return_driver(wrapper):
    """Return a driver to the pool with thorough cleanup"""
    if wrapper.driver_id == "temp":
        force_cleanup_driver(wrapper)
    else:
        try:
            # More thorough cleanup
            wrapper.driver.execute_script("window.stop();")
            wrapper.driver.execute_script("window.localStorage.clear();")
            wrapper.driver.execute_script("window.sessionStorage.clear();")
            wrapper.driver.delete_all_cookies()
            
            # Navigate to blank page to free memory
            wrapper.driver.get("about:blank")
            
            # Force garbage collection
            wrapper.driver.execute_script("if (window.gc) { window.gc(); }")
            
            DRIVER_POOL.put(wrapper, timeout=1)
        except Exception as e:
            print(f"Error returning driver to pool: {e}")
            force_cleanup_driver(wrapper)
            
            # Create replacement driver
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
            force_cleanup_driver(wrapper)
        except:
            pass
    
    # Kill any remaining Chrome processes
    kill_chrome_processes()

def get_clean_bing_links(driver, link):
    """Get clean Bing links with timeout protection"""
    try:
        driver.execute_script("window.open(arguments[0]);", link)
        driver.switch_to.window(driver.window_handles[-1])
        
        # Wait with timeout
        start_time = time.time()
        while time.time() - start_time < 5:  # 5 second timeout
            try:
                clean_link = driver.current_url
                if clean_link != "about:blank" and clean_link != link:
                    break
            except:
                pass
            time.sleep(0.1)
        else:
            clean_link = link  # Fallback to original link
        
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
        return clean_link
    except Exception as e:
        print(f"Error getting clean link for {link}: {e}")
        # Try to recover
        try:
            if len(driver.window_handles) > 1:
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
        except:
            pass
        return link

def extract_clean_text(driver):
    """Extract clean text with memory-conscious approach"""
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, "p, blockquote")
        list_elements = driver.find_elements(By.CSS_SELECTOR, "li")
        
        # Process in chunks to avoid memory buildup
        text_parts = []
        for el in elements + list_elements:
            try:
                text = el.text.strip()
                if text and len(text) > 10:  # Only meaningful text
                    text_parts.append(text)
                    # Limit total length early
                    if len(' '.join(text_parts)) > 4000:
                        break
            except:
                continue
        
        return ' '.join(text_parts)[:4000]
    except Exception as e:
        print(f"Error extracting text: {e}")
        return ""

def scrape_with_requests(url, sentence_cleaned):
    """Scrape with requests - more memory efficient"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=8, stream=True)
        
        # Limit response size
        max_size = 1024 * 1024  # 1MB limit
        content = ""
        for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
            content += chunk
            if len(content) > max_size:
                content = content[:max_size]
                break
        
        soup = BeautifulSoup(content, 'html.parser')
        
        # Remove unnecessary elements to save memory
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        
        paragraphs = soup.find_all(['p', 'blockquote']) 
        less_important_stuff = soup.find_all(['li'])
        elements_split = []

        for elem in paragraphs + less_important_stuff:
            try:
                for br in elem.find_all("br"):
                    br.replace_with("\n")
                
                lines = elem.text.split('\n')
                clean_lines = '\n'.join([line.strip() for line in lines if line.strip()])
                if clean_lines:
                    elements_split.append(clean_lines)
                    
                # Early break to save memory
                if len(elements_split) > 100:
                    break
            except:
                continue

        text = '\n'.join([e.strip() for e in elements_split if e.strip()])[:4000]

        print(f"TEXT FROM REQUESTS ({len(text)} chars):")
        print(text[:200] + "..." if len(text) > 200 else text)
        
        # Clean up
        del soup, content, elements_split
        gc.collect()
        
        if text and sentence_cleaned not in text:
            return {"clean_link": url, "text_cleaned": text}
        return {"clean_link": "", "text_cleaned": ""}
    except Exception as e:
        print(f"Error scraping {url} with requests: {e}")
        return {"clean_link": "", "text_cleaned": ""}

def periodic_cleanup():
    """Periodically clean up memory"""
    while True:
        time.sleep(300)  # Every 5 minutes
        print("Running periodic cleanup...")
        gc.collect()
        kill_chrome_processes()
        log_memory_usage()

@app.route('/get_results')
def get_results():
    # Initialize pool if not already done (lazy initialization)
    if not POOL_INITIALIZED:
        initialize_driver_pool()
    
    query = request.args.get('query')
    sentence = request.args.get('sentence')
    starting_index = int(request.args.get('starting_index', 0))
    sentence_cleaned = " ".join(sentence.split())
    
    # Default to lightweight mode for better memory management
    use_lightweight = request.args.get('lightweight', 'true').lower() == 'true'

    def generate():
        thread_id = threading.get_ident()
        cancel_event = threading.Event()
        
        with STREAMS_LOCK:
            ACTIVE_STREAMS[thread_id] = cancel_event
        
        REQUEST_SEMAPHORE.acquire()
        
        driver_wrapper = None
        try:
            print(f"Processing request - Memory: {log_memory_usage():.1f}MB")
            
            if not use_lightweight:
                print("Getting driver from pool...")
                driver_wrapper = get_driver()
                driver = driver_wrapper.driver
                print(f"Using driver {driver_wrapper.driver_id} (usage: {driver_wrapper.usage_count})")
            
            try:
                if use_lightweight:
                    # Use requests-only approach
                    print("Using lightweight requests-only mode")
                    search_url = f"https://www.bing.com/search?q={requests.utils.quote(query)}"
                    
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                    
                    response = requests.get(search_url, headers=headers, timeout=10)
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Extract search result links
                    result_links = []
                    for link_elem in soup.select('h2 a[href]'):
                        href = link_elem.get('href')
                        if href and href.startswith('http'):
                            result_links.append(href)
                    
                    links = result_links[starting_index:starting_index + 5]
                    
                else:
                    # Use Selenium approach
                    driver.set_page_load_timeout(12)
                    driver.get("https://www.bing.com/")
                    time.sleep(1)

                    print("Page loaded, looking for reject button...")
                    try:
                        reject_btn = WebDriverWait(driver, 1).until(
                            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reject') or contains(text(), 'Decline')]"))
                        )
                        reject_btn.click()
                        time.sleep(0.5)
                    except:
                        pass

                    search_box = driver.find_element(By.NAME, "q")
                    search_box.send_keys(query)
                    search_box.send_keys(Keys.RETURN)
                    time.sleep(1)

                    results = driver.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")
                    print(f"Found {len(results)} results")
                    
                    raw_links = [el.get_attribute("href") for el in results[starting_index:starting_index + 5]]
                    links = [get_clean_bing_links(driver, link) for link in raw_links]
                
                print(f"Processing {len(links)} links")
                
                for i, link in enumerate(links):
                    if cancel_event.is_set():
                        break
                    
                    yield "data: PROCESSING\n\n"
                    
                    try:
                        print(f"Processing link {i+1}/{len(links)}: {link[:60]}...")
                        
                        if use_lightweight:
                            result = scrape_with_requests(link, sentence_cleaned)
                            yield json.dumps(result) + "\n"
                        else:
                            # Clear browser state
                            driver.execute_script("window.localStorage.clear();")
                            driver.execute_script("window.sessionStorage.clear();")
                            driver.delete_all_cookies()
                            
                            driver.get(link)
                            time.sleep(1)

                            # Handle cookie consent
                            try:
                                accept_btn = WebDriverWait(driver, 2).until(
                                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept all') or contains(text(), 'Accept')]"))
                                )
                                accept_btn.click()
                                time.sleep(0.5)
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
                    
                    # Force cleanup between requests
                    if i % 2 == 0:  # Every 2 requests
                        gc.collect()

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
            
            # Cleanup
            gc.collect()
            print(f"Request completed - Memory: {log_memory_usage():.1f}MB")

    return Response(generate(), mimetype='text/event-stream')

# Health check endpoint with memory info
@app.route('/health')
def health_check():
    memory_mb = log_memory_usage()
    return {
        "status": "ok", 
        "pool_initialized": POOL_INITIALIZED,
        "memory_mb": memory_mb,
        "pool_size": DRIVER_POOL.qsize() if POOL_INITIALIZED else 0
    }

# Cleanup endpoint for manual memory cleanup
@app.route('/cleanup')
def manual_cleanup():
    kill_chrome_processes()
    gc.collect()
    memory_mb = log_memory_usage()
    return {"status": "cleanup_complete", "memory_mb": memory_mb}

# Register cleanup function
atexit.register(cleanup_drivers)

# Start periodic cleanup thread
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

if __name__ == '__main__':
    # Initialize pool before starting the server
    initialize_driver_pool()
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}")
    log_memory_usage()
    app.run(host='0.0.0.0', port=port, debug=False)

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


app = Flask(__name__)
CORS(app, resources={r"/get_results": {"origins": "*"}})

# Semaphore to limit concurrent requests to 5
REQUEST_SEMAPHORE = threading.Semaphore(5)

# Dictionary to track active streams by thread ID
ACTIVE_STREAMS = {}
STREAMS_LOCK = threading.Lock()

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
    chrome_options.page_load_strategy = 'eager'

    service = Service('/usr/lib/chromium-browser/chromedriver')

    # Tell Selenium this is Chromium, not Chrome
    return webdriver.Chrome(service=service, options=chrome_options)

def get_clean_bing_links(driver, link):
    driver.execute_script("window.open(arguments[0]);", link)  # Open link in new tab
    driver.switch_to.window(driver.window_handles[-1])  # Switch to new tab
    time.sleep(3)  # Allow the redirect to complete
    clean_link = driver.current_url  # Get final resolved URL
    driver.close()  # Close the new tab
    driver.switch_to.window(driver.window_handles[0])  # Switch back to main tab
    return clean_link


# Faster text extraction
def extract_clean_text(driver):
    elements = driver.find_elements(By.CSS_SELECTOR, "p, blockquote")
    list_elements = driver.find_element(By.CSS_SELECTOR, "li")
    combined = " ".join(el.text.strip() for el in elements + list_elements if el.text.strip())
    return combined[:4000]  # Slice only once at the end

# Lightweight alternative using BeautifulSoup
def scrape_with_requests(url, sentence_cleaned):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract text from relevant elements
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

        print("TEEEEXT")
        print(text[:600])
        
        if text and sentence_cleaned not in text:
            return {"clean_link": url, "text_cleaned": text}
        return {"clean_link": "", "text_cleaned": ""}
    except Exception as e:
        print(f"Error scraping {url} with requests: {e}")
        return {"clean_link": "", "text_cleaned": ""}

@app.route('/get_results')
def get_results():
    query = request.args.get('query')
    sentence = request.args.get('sentence')
    starting_index = int(request.args.get('starting_index', 0))
    sentence_cleaned = " ".join(sentence.split())
    
    use_lightweight = request.args.get('lightweight', 'true').lower() == 'true'

    def generate():
        thread_id = threading.get_ident()
        cancel_event = threading.Event()
        
        # Register this stream
        with STREAMS_LOCK:
            ACTIVE_STREAMS[thread_id] = cancel_event
        
        # Acquire semaphore to limit concurrent requests
        REQUEST_SEMAPHORE.acquire()
        
        try:
            print("here")
            DRIVER = None
            try:
                DRIVER = create_driver()
                DRIVER.set_page_load_timeout(15)  
                
                DRIVER.get("https://www.bing.com/")

                time.sleep(2) 

                print(DRIVER.page_source[:2000])

                try:
                    reject_btn = WebDriverWait(DRIVER, 1).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reject') or contains(text(), 'Decline')]"))
                    )
                    reject_btn.click()
                    time.sleep(1) 
                except:
                    pass 

                search_box = DRIVER.find_element(By.NAME, "q")
                search_box.send_keys(query)
                search_box.send_keys(Keys.RETURN)
                time.sleep(1) 

                results = DRIVER.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")
                print("RESULTS")
                print(results)
                links = [el.get_attribute("href") for el in results[starting_index:starting_index + 5]]
                links = [get_clean_bing_links(DRIVER, link) for link in links]
                
                if use_lightweight and DRIVER:
                    DRIVER.quit()
                    DRIVER = None
                
                print("LINKS")
                print(links)
                for link in links:
                    print("LINK")
                    print(link)

                    if cancel_event.is_set():
                        break
                    
                    yield "data: PROCESSING\n\n"
                    
                    try:
                        if use_lightweight:
                            result = scrape_with_requests(link, sentence_cleaned)
                            yield json.dumps(result) + "\n"
                        else:
                            if DRIVER:
                                DRIVER.execute_script("window.localStorage.clear();")
                                DRIVER.execute_script("window.sessionStorage.clear();")
                                DRIVER.delete_all_cookies()
                                
                                DRIVER.get(link)
                                time.sleep(1) 

                                try:
                                    accept_btn = WebDriverWait(DRIVER, 2).until(
                                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept all') or contains(text(), 'Accept')]"))
                                    )
                                    accept_btn.click()
                                    time.sleep(1)
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
        
        finally:
            # Clean up: remove from active streams and release semaphore
            with STREAMS_LOCK:
                ACTIVE_STREAMS.pop(thread_id, None)
            REQUEST_SEMAPHORE.release()

    return Response(generate(), mimetype='text/event-stream')
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
